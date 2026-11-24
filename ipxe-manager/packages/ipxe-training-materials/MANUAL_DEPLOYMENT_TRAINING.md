# iPXE 双栈装机服务内部部署 SOP

适用环境：Ubuntu Server 22.04/24.04、x86-64、systemd-networkd。本文以当前服务器的两个独立装机口为例，讲解如何从零部署、验证和维护。学员应具备 Linux 命令、IP 子网和 systemd 基础。

> 本 SOP 以 Python 3 管理程序为部署入口。它包含输入校验、部署快照和失败恢复；部署服务器需要 Python 3.10 或更新版本，不需要 `pip`。

## 1. 先理解启动链路

客户端网络启动分成两个阶段：

```text
客户端固件
  ├─ Legacy BIOS IPv4 PXE ──TFTP──> ipxe.pxe
  ├─ UEFI IPv4 PXE ─────────TFTP──> snponly.efi
  ├─ UEFI IPv6 PXE ─────────TFTP──> snponly.efi
  └─ UEFI HTTP Boot ─────────HTTP──> snponly.efi
                                      │
                                      ▼
                                    iPXE
                                      │ HTTP
                                      ▼
                              菜单、内核、initrd、ISO
```

第一阶段由主板或网卡固件完成。第二阶段由 iPXE 完成，使用 HTTP 下载大文件，速度和稳定性通常优于全程 TFTP。

### 启动原理与检查要点：客户端启动阶段

以 Legacy IPv4 为例，客户机网卡的 PXE 固件先发 DHCPDISCOVER。服务端返回客户端 IP、TFTP 服务器地址以及 `ipxe.pxe` 文件名。固件通过 TFTP 把这个文件下载到内存并执行。`ipxe.pxe` 是带原生网卡驱动的 iPXE 程序，不是 Ubuntu 内核；它接管网络启动后再向服务端请求配置，通过 HTTP 取得菜单。用户选择 Ubuntu 后，iPXE 再下载 `vmlinuz`、`initrd`，由 Linux 内核启动安装环境。部分固件提供的 UNDI 接口不完整，使用 `undionly.kpxe` 会出现 `PXE-EC8` 后退出，因此本项目默认使用 `ipxe.pxe`。

UEFI 的差别主要在第一个可执行文件：UEFI 固件执行 `.efi`，本项目的 Legacy BIOS 执行 `.pxe`。UEFI IPv6 固件先通过 IPv6 邻居发现、RA 和 DHCPv6 获取网络与 bootfile URL，然后下载 EFI 程序。iPXE 后续使用 IPv6 HTTP URL；地址写在 URL 中时必须用方括号，例如 `http://[fd42:19:10::1]:8080/menus/...`，否则冒号会与端口分隔符混淆。

执行 SOP 时要区分三个阶段：固件拿到地址、iPXE 显示菜单、OS 安装器真正启动。前一阶段成功不代表后一阶段成功。尤其是 ISO 下载和 initramfs 网络配置，发生在 iPXE 之后，必须分别确认。iPXE 官方的[链式启动说明](https://ipxe.org/howto/chainloading)也把固件加载 iPXE 与 iPXE 加载真实启动脚本分为两个阶段。

本项目支持 Legacy BIOS + IPv4、x86-64 UEFI + IPv4、x86-64 UEFI + IPv6。传统 BIOS 没有通用的原生 IPv6 PXE 标准，因此不要把“iPXE 本身支持 IPv6”误解成“所有 Legacy 固件都能从 IPv6 启动”。Secure Boot 开启时，自编译且未签名的 iPXE 通常无法运行。

## 2. 当前实验网络

| 用途 | 网口 | IPv4 | IPv6 |
|---|---|---|---|
| 装机网络 1 | `enp90s0f0` | `172.22.10.1/24` | `fd42:19:10::1/64` |
| 装机网络 2 | `enp90s0f1` | `172.22.11.1/24` | `fd42:19:11::1/64` |
| 管理网络 | `enx00e04c580a4c` | `10.10.12.19/23` | 保留原配置 |

两个装机口必须位于彼此隔离的二层网络。管理口不运行本项目的 DHCP、RA 或 TFTP。装机网络不发布默认网关和 DNS，客户端只能访问同一装机网中的服务器；这是离线安装场景的预期设计。

### 网络原理与操作要点：装机网隔离

DHCPv4 的初始请求通常是广播；RA 是 IPv6 链路本地的路由通告。交换机在同一个 VLAN 内转发这些报文。若把两个装机口接进同一个 VLAN，可能出现同一客户端收到两套不同子网的租约，故障具有随机性。独立接口配独立 VLAN、独立 IPv4 网段和独立 IPv6 `/64`，可以从网络拓扑上消除冲突。

`fd42:...` 是站点内部使用的 IPv6 ULA 地址。这里的用途是同一链路上的装机客户端访问服务端，不要求公网可路由，因此没有必要借用管理网的 IPv6 前缀。IPv6 `/64` 还用于 SLAAC；dnsmasq 同时提供 RA 和 DHCPv6。RA 告诉客户端这个链路的前缀及是否有路由器；DHCPv6 可分配地址与引导 URL。本项目将 RA 的路由器生存期设为 0，表示装机服务器不充当外网网关。

管理口承载 SSH 与默认路由。一旦把它改成装机网地址、停掉原 DHCP 或发出新的 RA，管理员可能断开连接，生产网客户端也可能收到错误配置。程序因此检查默认路由和 SSH 会话对应网卡，拒绝在该网卡部署装机服务。部署前先通过 `ip -br address` 与 `ip route` 识别管理口，再确认配置文件中的 `enabled=false`。

查看真实网口和管理路由：

```bash
ip -br link
ip -br address
ip -4 route show default
ip -6 route show default
```

不要把当前 SSH 或默认路由所在接口加入装机 DHCP 配置。

## 3. 准备软件和目录

```bash
apt-get update
apt-get install -y --no-install-recommends \
  dnsmasq-base nginx-core git build-essential liblzma-dev \
  xorriso ca-certificates curl unzip

install -d -m 755 \
  /opt/ipxe-manager \
  /etc/ipxe-manager/os.d \
  /srv/ipxe/http/boot \
  /srv/ipxe/http/menus \
  /srv/ipxe/http/os \
  /var/lib/ipxe-manager \
  /var/log/ipxe-manager
```

组件职责：

| 组件 | 作用 |
|---|---|
| dnsmasq | DHCPv4、DHCPv6、IPv6 RA 和 TFTP |
| nginx | HTTP 分发菜单、内核、initrd 和 ISO |
| iPXE | 第二阶段网络引导及交互菜单 |
| systemd-networkd | 为装机口持久配置静态双栈地址 |
| `ipxe-manager.py` | 校验、生成、构建、导入 OS、回滚 |

### 组件职责与操作要点

dnsmasq 的任务是回答“我是谁、我从哪里取第一阶段程序”。TFTP 足以传输相对小的 iPXE 程序；它在 PXE 固件中有广泛支持。nginx 的任务是传输菜单、内核、initrd 和 GB 级 ISO；这些文件通过 HTTP 下载更合适。systemd-networkd 为每个装机接口配置服务端固定地址，让 DHCP 返回的服务器地址以及菜单中的 URL 在重启后仍有效。systemd 负责开机启动、失败重启和日志。Python 3 管理程序把这些组件的配置从一份 JSON 生成，避免管理员分别改多个文件导致地址不一致。

`dnsmasq-base` 提供 dnsmasq 程序，项目使用独立的 `ipxe-dhcp.service`，不会把装机配置混入发行版默认 dnsmasq 实例。nginx 也有独立配置和 `ipxe-http.service`；HTTP 监听 8080，两个装机地址族均可访问。`xorriso` 用于从 Ubuntu ISO 提取内核、initrd，导入 RHEL 系镜像时还会提取安装树。`git`、编译器和开发库仅用于首次构建或变更引导文件。

目录分工也有意义：`/etc/ipxe-manager` 是人工维护的配置；`/srv/ipxe/http` 是对客户端发布的文件；`/var/lib/ipxe-manager` 保存构建源码、租约、备份和回收区；`/var/log/ipxe-manager` 保存 HTTP 日志。把公开目录与配置、备份分开，避免客户端通过 HTTP 下载管理配置或历史文件。

## 4. 安装管理程序

把 `ipxe-manager.zip` 复制到服务器后解压。下面示例假设文件位于 `/tmp`：

```console
unzip -q /tmp/ipxe-manager.zip -d /tmp
cd /tmp/ipxe-manager
sudo python3 install.py
```

`install.py` 会把管理程序安装到 `/opt/ipxe-manager/ipxe-manager.py`，建立命令入口 `/usr/local/sbin/ipxe-manager`，并运行首次部署。它只在服务器没有配置文件时复制 `config.example.json`，不会覆盖现有 `/etc/ipxe-manager/config.json`。初次使用前应先核对示例中的网口、地址和 DHCP 地址池。

查看 Python 版本、命令帮助和 JSON 格式：

```console
python3 --version
ipxe-manager --help
python3 -m json.tool /etc/ipxe-manager/config.json
```

管理器使用 Python 标准库的 `json`、`ipaddress`、`hashlib` 等模块，不需要 `pip`、`jq`、`sipcalc` 或 `grepcidr`。程序会通过安全的参数列表调用 `apt-get`、`xorriso`、`git`、`make`、`systemctl`、`networkctl` 等系统工具；这些工具分别负责安装系统组件、提取 ISO、编译 iPXE 及应用服务和网卡配置。

### 管理入口与更新方式

实际逻辑保存在 `/opt/ipxe-manager/ipxe-manager.py`，`/usr/local/sbin/ipxe-manager` 是指向它的命令入口。更新时替换 Python 程序即可，日常命令仍使用 `ipxe-manager`。

首次部署由程序安装 `dnsmasq`、`nginx`、`xorriso`、`git`、编译工具和其他系统依赖，因此需要可用的 Ubuntu/Debian 软件源。Python 本身不需要第三方包。

## 5. 编写唯一配置源

保存为 `/etc/ipxe-manager/config.json`：

```json
{
  "schema": 1,
  "http_port": 8080,
  "ipxe_commit": "7cd92e01d6514b2c8091999f9bce8aef22a1d586",
  "networks": [
    {
      "interface": "enp90s0f0",
      "enabled": true,
      "configure_address": true,
      "ipv4": "172.22.10.1/24",
      "pool4": ["172.22.10.100", "172.22.10.220"],
      "ipv6": "fd42:19:10::1/64",
      "pool6": ["fd42:19:10::100", "fd42:19:10::ffff"]
    },
    {
      "interface": "enp90s0f1",
      "enabled": true,
      "configure_address": true,
      "ipv4": "172.22.11.1/24",
      "pool4": ["172.22.11.100", "172.22.11.220"],
      "ipv6": "fd42:19:11::1/64",
      "pool6": ["fd42:19:11::100", "fd42:19:11::ffff"]
    },
    {
      "interface": "enx00e04c580a4c",
      "enabled": false,
      "note": "Management NIC; never serve provisioning DHCP here."
    }
  ]
}
```

```console
chmod 600 /etc/ipxe-manager/config.json
python3 -m json.tool /etc/ipxe-manager/config.json >/dev/null
```

配置原则：每个启用网口使用独立 IPv4 子网和独立 IPv6 `/64`；服务器地址不能落在 DHCP 池内；不同网口子网不能重叠。`configure_address=false` 表示地址由其他网络管理工具配置，程序仍会检查地址是否已经存在。

### 配置字段与校验规则

`schema` 是配置格式版本，程序拒绝不认识的版本，防止新旧程序把同一个字段解释成不同含义。`http_port` 是客户端实际访问的端口，修改它会影响 nginx 监听、嵌入 iPXE 的引导 URL 和菜单 URL，不能只改 nginx。`ipxe_commit` 固定源码提交号；不同日期部署仍编译同一版本，有利于复现故障和核对文件 SHA256。

每个 `networks` 对象代表一个网口。`interface` 必须与 `ip link` 中的名称精确一致。`enabled=false` 让管理口只出现在清单中，不出现在 DHCP、RA、TFTP 监听配置。`configure_address=true` 让程序生成一个只匹配该接口的 `.network` 文件；现有网络管理方案已为接口配置静态地址时可改为 `false`，但必须保证地址真实存在。

`ipv4`/`ipv6` 是服务器自身的地址和掩码；`pool4`/`pool6` 是分给客户端的可用范围。例如服务端为 `172.22.10.1/24`，地址池为 `.100` 到 `.220`，两者同属 `172.22.10.0/24`，但不会把服务器地址租给客户端。IPv6 同理，`fd42:19:10::1/64` 是服务器，`::100` 到 `::ffff` 是客户端地址池。程序还检查格式、地址族、网段重叠和服务器地址误入池内；`json.tool` 只验证 JSON 语法，不替代这些语义检查。

`chmod 600` 使配置仅 root 可读写。当前地址本身不是秘密，但统一限制管理配置权限有利于将来增加敏感参数；生成的菜单与镜像则放在对客户端可读的发布目录中。

## 6. 预览生成配置

在修改系统前先渲染到临时目录：

```bash
preview_dir=$(mktemp -d /tmp/ipxe-preview.XXXXXX)
ipxe-manager --config /etc/ipxe-manager/config.json \
  render --output "$preview_dir"

find "$preview_dir" -maxdepth 1 -type f -print
dnsmasq --test --conf-file="$preview_dir/dnsmasq.conf"
nginx -t -c "$preview_dir/nginx.conf"
```

部署检查时重点查看 `dnsmasq.conf`：

- DHCP option 93 的 `0` 代表 Legacy BIOS；`7/9` 代表 x86-64 UEFI；`16` 代表 UEFI HTTP Boot。
- 第一次 DHCP 给固件下发 iPXE 二进制。
- iPXE 再次 DHCP 会携带 user-class 或 option 175，此时下发 HTTP 菜单，避免循环加载自身。
- DHCPv6 option 59 提供 IPv6 TFTP 或 HTTP URL。
- IPv6 URL 中地址必须写成 `[fd42:19:10::1]`。

### 预览文件的检查方法

`render` 是纯生成动作：读取 JSON，按网口展开配置，写入管理员指定的预览目录，不安装服务，也不改变系统网络。这里可把预览当成“变更前的施工图”。`dnsmasq --test` 与 `nginx -t` 检查各自配置语法；它们不能证明物理网线已连接、交换机 VLAN 正确或客户端固件兼容。执行部署检查时，先确认静态地址对应的 `.network` 文件、Legacy 的 `.pxe` 分发规则、IPv6 UEFI 的 `.efi` 分发规则，以及 HTTP 8080 使用的菜单，再运行语法检查。

DHCPv4 option 93 是客户端架构标识；同一台服务端不能把 BIOS 可执行文件发给 UEFI 客户端。服务端先按架构选文件，再按客户端是否已经进入 iPXE 决定返回二进制还是菜单。iPXE 自身通常发送 user-class `iPXE`，程序还识别 option 175。若没有第二次请求识别，iPXE 又收到自身的 `.pxe`/`.efi`，就会重新加载，形成无限循环；[iPXE user-class 文档](https://ipxe.org/cfg/user-class)有这一机制的原理说明。

DHCPv6 与 DHCPv4 不是把 IP 字符串换长这么简单。UEFI IPv6 固件通过 DHCPv6 的架构选项识别 EFI 类型，再由 option 59 接收 bootfile URL。IPv6 邻居发现/RA 负责链路层发现和前缀通告。项目明确不发送一个长度为 0 的 DHCPv6 DNS 选项，因为固件可能把它当成非法响应；这里只用数字地址，根本不需要 DNS 解析。

预览目录是一次性草稿；`mktemp -d` 给它分配独立名字，避免并发演练互相覆盖。删除时只删除本次保存到 `preview_dir` 的目录，不要对未核对路径执行递归清理。

查看完删除预览目录：

```bash
rm -r -- "$preview_dir"
```

## 7. 执行部署

`python3 install.py` 已在首次安装时自动运行部署。以后修改了网口或网络配置，需要重新应用时执行：

```bash
ipxe-manager deploy
```

依赖已安装后的重复部署：

```bash
ipxe-manager deploy --skip-packages
```

部署过程会：

1. 检查发行版、网口、默认路由、SSH 接口和现有 DHCP 服务。
2. 在 `/var/lib/ipxe-manager/backups/` 保存配置与引导文件快照。
3. 固定 iPXE Git commit，开启 IPv6，编译每个网口的引导文件。
4. 生成 networkd、dnsmasq、nginx 和 systemd 配置。
5. 只重配指定装机接口，不全局重启网络。
6. 启动并设为开机自启，最后检查 HTTP 菜单。
7. 任一步骤失败时尝试恢复本次部署前快照。

### 部署顺序与检查点

首先验证宿主机环境。若网口不存在、两个网络重叠，或指定装机口其实承载当前 SSH/默认路由，后续步骤必须停止。这样可以在修改系统之前发现风险。随后把旧配置、旧 systemd 单元、旧网络配置和旧引导文件记录到独立快照目录。快照名字包含时间和进程号，是用于恢复的一次部署边界，不是新的 OS 或密钥。

构建 iPXE 时，程序检出配置里指定的 Git commit，并编译每个网口的三个文件：`-bios.pxe`、`-uefi4.efi`、`-uefi6.efi`。构建时把当前网口和 IP 地址族的 HTTP 菜单 URL 嵌入引导程序，因此新增网口或修改地址/HTTP 端口，需要重新构建；仅增加 OS 条目时，菜单会更新，引导二进制可复用。程序记录每个文件的 SHA256，重复部署先校验现有文件，再决定是否重新编译。

生成配置后，先对 dnsmasq/nginx 做语法检查，再发布到正式目录。装机口的 `.network` 文件名称以 `05-ipxe-` 开头，系统按文件名顺序匹配网络配置；这里让它优先于系统安装器生成的通用网卡配置。程序使用 `networkctl reload` 加上指定接口的 `networkctl reconfigure`，不重启整个网络栈，以保护管理连接。[systemd.network 文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.network.html)说明了文件匹配与排序规则。

最后启动 `ipxe-http.service` 和 `ipxe-dhcp.service`，并访问每个网口的菜单 URL。静态 IPv6 地址在物理网口无载波时可能尚不能完成邻居发现/重复地址检测，因此状态会标记 `PENDING physical link`；这不是把未接线误报为整个部署失败。若中途抛错，程序恢复配置快照并尽量恢复原服务状态，但断电和 `kill -9` 不属于可保证自动恢复的情况。

部署后检查：

```bash
ipxe-manager status
systemctl status ipxe-dhcp.service ipxe-http.service --no-pager
ip -br address show enp90s0f0
ip -br address show enp90s0f1
ss -luntp | grep -E ':(67|69|547|8080)\b'
```

未接网线时接口显示 `DOWN` 或 `NO-CARRIER` 是正常的；静态地址仍应存在。

让学员把几个检查结果对应到层次：`ip -br address` 验证服务端地址；`systemctl` 验证进程；`ss` 验证监听端口；`curl` 验证 HTTP 应用。它们依次增加信心，但仍不能证明客户端网卡能从真实交换机收到 DHCP/RA。只有连接隔离网的客户端实测才能验证链路层与固件阶段。

## 8. 导入 Ubuntu 24.04

当前 ISO：

```text
/mnt/ubuntu-24.04-live-server-amd64.iso
```

执行：

```bash
ipxe-manager add-os ubuntu-24-04 \
  --kind ubuntu-live \
  --source /mnt/ubuntu-24.04-live-server-amd64.iso \
  --title 'Ubuntu 24.04 Server'
```

程序会计算 SHA256、提取 `vmlinuz` 和 `initrd`、复制 ISO，然后原子发布配置并刷新全部菜单。Ubuntu 24.04 的 initramfs 已确认包含 DHCPv6 支持：IPv4 菜单使用 `ip=dhcp`，IPv6 菜单使用 `ip=dhcp6`。

### 内核、initrd 与完整 ISO 的用途

`vmlinuz` 是 Linux 内核，负责接管硬件、启动初期驱动并挂载初始内存文件系统。`initrd` 是初期用户空间，其中包含网络配置、下载安装介质和启动 live 环境所需的程序。完整 ISO 则提供安装环境及实际软件内容。只上传 ISO 而不单独提供内核和 initrd，iPXE 无法按当前菜单直接启动；只提供内核/initrd 而没有 ISO，安装环境通常无法取得完整安装源。

导入命令先计算源 ISO 的 SHA256，再在暂存目录提取 `/casper/vmlinuz` 与 `/casper/initrd`，复制 ISO，完成检查后才把目录发布到 `/srv/ipxe/http/os/ubuntu-24-04` 并生成 OS JSON。这里的“原子发布”主要指配置文件/菜单与完成后的目录切换，不代表把数 GB 的复制操作本身变成瞬时操作。新镜像尚未完整导入时不会提前出现在菜单里。

菜单按网口和地址族生成四份。同一个 Ubuntu 条目会出现在两个 IPv4 和两个 IPv6 菜单中。IPv4 菜单的内核参数是 `ip=dhcp`；IPv6 菜单是 `ip=dhcp6`；两者的 ISO URL 使用各自装机接口的服务器地址。Ubuntu 24.04 ISO 的 initramfs 中已检查到 DHCPv6 处理代码，但完整安装仍需要目标机器实测，不能只因菜单生成成功就宣称操作系统安装完成。

`--kind ubuntu-live` 选择 Ubuntu Live Server 模板；RHEL 系 DVD 使用另一种安装树模板；Windows/WinPE 需要自定义启动脚本，不能把任意 ISO 都当成 Ubuntu ISO 导入。OS id `ubuntu-24-04` 是目录名和菜单内部标识，应保持稳定、不要包含斜杠或空格。

验证：

```bash
ipxe-manager list-os
find /srv/ipxe/http/os/ubuntu-24-04 -maxdepth 1 -type f -ls
grep -A5 ':os-ubuntu-24-04' /srv/ipxe/http/menus/*.ipxe
sha256sum \
  /mnt/ubuntu-24.04-live-server-amd64.iso \
  /srv/ipxe/http/os/ubuntu-24-04/image.iso
```

两个 SHA256 应相同。新增 OS 不需要重启 DHCP 服务。

## 9. 客户端固件设置与测试顺序

先关闭 Secure Boot，再分别测试：

1. Legacy/CSM + IPv4 PXE。
2. UEFI + IPv4 PXE。
3. UEFI + IPv6 PXE。
4. 固件支持时再测试 UEFI HTTP Boot。

每次测试只连接一个装机网络，避免判断错误。预期画面顺序：固件获得地址、下载 iPXE、iPXE 配置网络、下载菜单、显示 `Ubuntu 24.04 Server`、启动 Ubuntu 安装器。

### 验证顺序与判断依据

先测 Legacy IPv4，能尽快验证最基础的 DHCPv4、TFTP 和 HTTP 链路；再测 UEFI IPv4，把启动文件由 `.pxe` 切换为 `.efi`，可以隔离固件架构问题；最后测 UEFI IPv6，会同时涉及 RA、DHCPv6、IPv6 TFTP/HTTP 及 Ubuntu initramfs 的 DHCPv6。若一上来直接测试最复杂的组合，出错时难以定位是物理链路、地址分配、引导程序还是安装器。

操作时在固件菜单明确选择 `PXE IPv4`、`PXE IPv6` 或 `HTTP Boot`，不要仅凭“Network Boot”名称猜测地址族。观察下载文件名能判断第一阶段是否匹配：Legacy 应是 `-bios.pxe`；UEFI IPv4 是 `-uefi4.efi`；UEFI IPv6 是 `-uefi6.efi`。iPXE 菜单上的 `IPv4`/`IPv6` 标题说明第二阶段使用的地址族。

启动到 Ubuntu 安装器后，还要确认它能下载 2.6 GB ISO，并能识别目标硬盘及网卡。自动安装/分区不在本 SOP 范围；现场验证可在安装器出现后退出，避免误清盘。

进入安装器不等于可以立刻无人值守清盘。本项目默认进入交互安装，没有附加 autoinstall 或自动擦盘参数。

## 10. OS 日常维护

```bash
# 查看
ipxe-manager list-os

# 暂时从菜单下架；文件保留，适合仍有客户端安装时使用
ipxe-manager disable-os ubuntu-24-04

# 恢复菜单
ipxe-manager enable-os ubuntu-24-04

# 移出菜单并移入可恢复的回收目录
ipxe-manager remove-os ubuntu-24-04

# 手工修改 OS JSON 后重建菜单
ipxe-manager refresh-menu
```

OS 配置位于 `/etc/ipxe-manager/os.d/`，数据位于 `/srv/ipxe/http/os/`，回收目录位于 `/var/lib/ipxe-manager/trash/`。不要直接删除仍被客户端安装使用的 ISO。

### 菜单状态和文件生命周期

`disable-os` 只把 OS 配置中的 `enabled` 改为 `false`，重新生成菜单。已经通过旧菜单开始下载 ISO 的客户端仍能访问原文件，所以适合维护期间暂停新安装。`enable-os` 把条目重新加入菜单。两者都不重新构建 iPXE，也不重启 DHCP/TFTP/HTTP 服务。

`remove-os` 比禁用更进一步：先从菜单中移除条目，再把 OS JSON 和镜像目录移到带时间戳的回收目录。它不会直接销毁数据，但正在下载安装的客户端可能因为原 URL 消失而失败。因此先禁用、观察 HTTP 日志确认没有在途下载，再移除。恢复时把回收目录中的 `data` 和 JSON 放回原位置，执行 `refresh-menu`。

把“增加 OS”理解为增加一条受管理的安装入口：目录放内容，JSON 描述类型、标题、启用状态和地址族，菜单从 JSON 生成。不要直接改生成的 `.ipxe` 菜单；下一次刷新会覆盖手工修改。

## 11. 日志和故障排查

实时查看 DHCP/TFTP：

```bash
journalctl -u ipxe-dhcp.service -f
```

查看 HTTP 请求：

```bash
tail -f /var/log/ipxe-manager/http-access.log
tail -f /var/log/ipxe-manager/http-error.log
```

常见现象：

| 现象 | 检查 |
|---|---|
| 客户端拿不到地址 | 网线、VLAN、接口是否 `LOWER_UP`；是否在正确装机广播域；UDP 67/547 |
| DHCP 出现前报 `PXE-EC8` | 客户端 PXE Option ROM 没有建立有效 `!PXE` 结构；检查 BIOS/网卡固件、Legacy Option ROM 与启动设置，此时服务端尚未下发文件 |
| 固件拿到地址但不下载 | 架构是否受支持；TFTP UDP 69 和 20000:20100；DHCP 日志中的 bootfile |
| iPXE 反复下载自身 | 检查 user-class/option 175 标签和生成的 dnsmasq 配置 |
| UEFI IPv6 报网络错误 | 固件是否启用 IPv6 PXE；RA/ND 是否被交换机或防火墙拦截；不能下发空 DHCPv6 DNS 选项 |
| 菜单能显示但 ISO 下载失败 | HTTP 8080、文件权限、磁盘空间、访问日志 |
| Ubuntu 下载 ISO 后失败 | 核对 ISO SHA256、内存容量、具体 ISO 版本和内核参数 |
| Secure Boot 拒绝 EFI | 关闭 Secure Boot，或另行部署可信签名链 |

### 故障定位：以最后成功阶段为界

执行故障定位时，结合客户端屏幕与服务端日志判断，不要只看 `systemctl active`：

1. **没有租约**：客户端固件还没到文件下载阶段。先查网线、交换机端口、VLAN、网卡固件 PXE 设置，再查 DHCPv4 UDP 67/68；IPv6 还需要 RA/ND 与 DHCPv6 UDP 546/547。服务端 `journalctl -u ipxe-dhcp` 应能看到 DISCOVER 或 SOLICIT。
2. **有租约但第一阶段文件取不到**：在 DHCP 日志里核对架构标签、bootfile 名称、next-server/URL。IPv4 PXE 的 TFTP 首包到 UDP 69，实际数据交换会使用动态端口；项目限定服务器端 TFTP 数据端口 20000–20100，防火墙也要允许。
3. **iPXE 已出现却没有菜单**：第一阶段引导已经成功。检查 iPXE DHCP 二次请求、user-class/option 175，接着查 nginx 是否记录 `/menus/网口-地址族.ipxe` 的 200 响应。若反复下载 `.efi`/`.pxe`，通常是二次请求没有识别而形成循环。
4. **菜单出现却无法下载内核或 ISO**：检查 `http-access.log` 中 OS 路径的状态码。404 表示 URL 或文件不一致；连接超时可能是地址族、路由、HTTP 端口或防火墙；下载中断还应检查网线质量、磁盘空间及服务进程。
5. **Ubuntu 内核启动后失败**：iPXE 已完成工作。此时查看内核命令行、`ip=dhcp`/`ip=dhcp6`、initramfs 输出、ISO SHA256 和目标机内存。IPv6 菜单可显示不代表 Ubuntu 的早期用户空间一定获得了 IPv6 地址。

排查时可用 `journalctl -u ipxe-dhcp -f` 观察第一、第二阶段请求，再查看 `/var/log/ipxe-manager/http-access.log` 中的菜单、内核和 ISO 请求。客户端每前进一个阶段，服务器日志会留下对应记录。

只读检查命令：

```bash
dnsmasq --test --conf-file=/etc/ipxe-manager/dnsmasq.conf
nginx -t -c /etc/ipxe-manager/nginx.conf
systemctl is-active ipxe-dhcp.service ipxe-http.service
curl --noproxy '*' http://172.22.10.1:8080/catalog.json
curl --noproxy '*' -g 'http://[fd42:19:10::1]:8080/catalog.json'
```

## 12. 回滚

列出部署快照：

```bash
ls -1dt /var/lib/ipxe-manager/backups/*
```

选择明确的快照目录后执行：

```bash
ipxe-manager rollback \
  /var/lib/ipxe-manager/backups/20260918-082137-326512
```

回滚会恢复服务配置、systemd-networkd 文件、服务原状态和引导文件，不删除 OS 镜像，也不卸载 apt 软件包。不要随便运行旧快照，否则可能恢复到过时网络配置。

### 快照恢复范围

每次 `deploy` 前会记录目标文件原来是否存在；存在则复制到快照，不存在则在回滚时移除新文件。它还记录两个服务原来的运行状态，保存引导文件目录。因此回滚的对象是“这一次网络与服务部署”，不是整台服务器的系统还原点。OS 导入产生的数据、apt 安装的软件包以及外部交换机配置不在快照里。

选快照时不能只看最新时间，应先明确“希望恢复到哪一次部署前”。同一个旧快照可能使用不同 IP/端口，与当前已导入的 OS 菜单不完全匹配。执行后再次运行 `ipxe-manager status`、查看管理 SSH 是否仍通，并核对网口地址和服务状态。

## 13. SOP 验收清单

- 能解释固件 PXE 与 iPXE 的两阶段关系。
- 能指出 Legacy 原生 IPv6 的限制。
- 能识别管理口并说明为什么不能在管理网启动第二套 DHCP。
- 能读懂 `config.json` 的服务器地址与地址池关系。
- 能在预览目录完成 dnsmasq/nginx 配置检查。
- 能部署服务并确认两个 systemd 单元为 `active/enabled`。
- 能导入、禁用、启用和回收 Ubuntu OS 条目。
- 能从日志判断失败发生在 DHCP、TFTP、iPXE、HTTP 还是 OS 安装器。
- 能选择正确部署快照执行回滚。

实施人员应先在隔离交换网络完成验证，再连接真实待装服务器。

## 14. 部署步骤与现场检查

部署流程按以下阶段顺序执行；具体时长由现场环境决定。

| 阶段 | 操作目标 | 检查依据 |
|---|---|---|
|---|---|---|
| 启动链路 | 区分固件、iPXE、Linux 内核和安装器 | 明确 `.pxe`、`.efi`、`vmlinuz`、`initrd`、ISO 的获取阶段 |
| 网络规划 | 保护管理口并隔离装机二层网络 | 对照 `ip -br address`、默认路由和网络配置 |
| 配置与预览 | 确认 JSON 到 dnsmasq/nginx/networkd 的映射 | 找出 BIOS、UEFI IPv4、UEFI IPv6 引导规则 |
| 部署与验收 | 逐项验证配置、服务和 HTTP 菜单 | 配置检查无误，服务为 active，菜单可访问 |
| OS 导入 | 确认内核、initrd 和安装源文件 | 核对菜单条目、ISO SHA256 和安装树 |
| 客户端实测 | 按 Legacy IPv4、UEFI IPv4、UEFI IPv6 排查 | 对照 DHCP 与 HTTP 日志定位最后成功阶段 |
| 维护与回滚 | 区分禁用、删除、回滚 | 在途安装优先禁用；按目标选择部署前快照 |

以下案例用于现场故障定位：

1. 客户端拿到 `172.22.10.150`，却去下载 `-uefi6.efi`：核对固件实际启动模式和 DHCP 架构标签；UEFI IPv4 应收到 `-uefi4.efi`。
2. iPXE 标志反复出现，服务端没有 HTTP `/menus/` 请求：检查二次 DHCP 请求识别，避免再次下发 iPXE 二进制。
3. IPv6 菜单正常出现，Ubuntu 启动后获取 ISO 失败：检查 initramfs 的 DHCPv6、ISO URL、HTTP 日志和链路；不要把已成功的第一阶段误判为服务端 PXE 故障。

完成 SOP 后，实施人员应能够解释 `config.json`、预览并校验生成配置、从日志判断启动停在哪个阶段、找到正确的部署快照。实机验证只在隔离装机网络开展，不把测试 DHCP 接入办公或生产网。



## 12. Other OS 与自定义镜像 SOP

### 12.1 先判断镜像类型

不要只看 ISO 文件名。先查看镜像内部目录：

```bash
xorriso -indev /root/镜像.iso -ls / 2>/dev/null
```

RHEL 兼容完整 DVD 至少应包含 `.treeinfo`、`images/pxeboot/`、`repodata/`，通常还会有 `Packages/`、`BaseOS/` 或 `AppStream/`。这类镜像使用 `--kind rhel`。Ubuntu Server Live 使用 `/casper/vmlinuz` 和 `/casper/initrd`，使用 `--kind ubuntu-live`。

只有 `EFI/BOOT`、`isolinux` 和少量 `images` 的 Boot ISO 通常不含软件仓库，不能按本地 DVD 方式导入。Boot ISO 需要另外提供 HTTP/YUM 安装树，并为它编写单独的 `boot.ipxe`。

### 12.2 RHEL 兼容完整 DVD

```bash
ipxe-manager add-os vesselos-1-0-sp1 \
  --kind rhel \
  --source /root/VesselOS-1.0-SP1-x86_64-dvd.iso \
  --title 'VesselOS 1.0 SP1'
```

导入后检查：

```bash
ipxe-manager list-os
curl -I http://172.22.10.1:8080/os/vesselos-1-0-sp1/tree/.treeinfo
```

### 12.3 Boot ISO 或目录结构不同的 Other OS

当前脚本不会猜测任意镜像的内核参数。客户需要准备一个目录，目录中必须有以 `#!ipxe` 开头的 `boot.ipxe`：

```text
/root/ipxe-custom/rescue/
├── boot.ipxe
├── boot/vmlinuz
└── boot/initrd.img
```

`boot.ipxe` 示例：

```ipxe
#!ipxe
set osbase http://${next-server}:8080/os/rescue
kernel ${osbase}/boot/vmlinuz initrd=initrd.img ip=dhcp rd.neednet=1
initrd ${osbase}/boot/initrd.img
boot
```

添加：

```bash
ipxe-manager add-os rescue \
  --kind custom \
  --source /root/ipxe-custom/rescue \
  --title 'Other OS Rescue'
```

验证：

```bash
curl http://172.22.10.1:8080/os/rescue/boot.ipxe
curl -I http://172.22.10.1:8080/os/rescue/boot/vmlinuz
curl -I http://172.22.10.1:8080/os/rescue/boot/initrd.img
```

`custom` 只负责发布目录并 chain `boot.ipxe`；内核路径、initrd 路径、`root=`、`inst.repo=`、`boot=` 等参数由镜像厂商或客户根据该 OS 的启动文档填写。只含 EFI 启动文件的 Windows PE 或特殊 ISO 不能直接套用 Linux 的 `kernel/initrd` 示例。

### 12.4 服务器端验收脚本

`ipxe-verify.py` 是只读检查脚本，不会改动网卡、DHCP 或 OS 配置：

```console
sudo python3 ipxe-verify.py
```

它检查服务、命令、装机口地址、OS 文件、RHEL 仓库索引、custom `boot.ipxe` 和生成的菜单。出现 `FAIL` 时先修复对应项目，再重复执行。它不能替代真实客户端的 Legacy/UEFI 和 IPv4/IPv6 启动测试。

## 13. 非标准 OS 手动接入 SOP

本节适用于目录结构不符合 `ubuntu-live` 或 `rhel` 模板的镜像，例如 Boot ISO、Debian Live、厂商定制 Linux、救援系统和其他只提供 EFI/isolinux 启动文件的介质。脚本不会自动猜测这类镜像的 `kernel`、`initrd`、`root` 或仓库参数，管理员必须根据镜像厂商文档确认启动方式。

### 13.1 判断镜像结构

先查看 ISO 根目录，不要只根据文件名判断：

```bash
xorriso -indev /root/other-os.iso -ls / 2>/dev/null
```

扫描常见启动文件：

```bash
xorriso -indev /root/other-os.iso \
  -find / -type f \( \
  -name 'vmlinuz*' -o -name 'linux*' -o \
  -name 'initrd*' -o -name 'initramfs*' -o \
  -name 'BOOTX64.EFI' \) -print 2>/dev/null
```

记录以下信息：

```text
内核路径：例如 /boot/vmlinuz 或 /install/vmlinuz
initrd 路径：例如 /boot/initrd.img 或 /install/initrd.gz
根文件系统参数：例如 root=、boot=、live-media-path=
安装源参数：例如 inst.repo=、url=、fetch=、iso-url=
网络参数：例如 ip=dhcp 或 ip=dhcp6
```

如果只有 `EFI/BOOT/BOOTX64.EFI`，不能直接套用 Linux 的 `kernel/initrd` 示例，需要使用该系统支持的 EFI/HTTP 启动方法。

### 13.2 建立发布目录

目录名使用稳定的 OS ID：

```bash
mkdir -p /root/ipxe-custom/other-os/boot
```

从 ISO 提取启动文件：

```bash
xorriso -osirrox on -indev /root/other-os.iso \
  -extract /boot/vmlinuz \
  /root/ipxe-custom/other-os/boot/vmlinuz

xorriso -osirrox on -indev /root/other-os.iso \
  -extract /boot/initrd.img \
  /root/ipxe-custom/other-os/boot/initrd.img
```

如果安装器还需要 squashfs、ISO 或其他目录，也要一并提取。例如：

```bash
xorriso -osirrox on -indev /root/other-os.iso \
  -extract /live \
  /root/ipxe-custom/other-os/live
```

最终目录至少应包含：

```text
/root/ipxe-custom/other-os/
├── boot.ipxe
├── boot/vmlinuz
├── boot/initrd.img
└── 其他安装文件
```

### 13.3 编写 boot.ipxe

```bash
nano /root/ipxe-custom/other-os/boot.ipxe
```

IPv4 示例：

```ipxe
#!ipxe
set osbase http://${next-server}:8080/os/other-os
kernel ${osbase}/boot/vmlinuz \
  initrd=initrd.img \
  ip=dhcp \
  rd.neednet=1
initrd ${osbase}/boot/initrd.img
boot
```

IPv6 示例：

```ipxe
#!ipxe
set osbase http://${next-server}:8080/os/other-os
kernel ${osbase}/boot/vmlinuz \
  initrd=initrd.img \
  ip=dhcp6 \
  rd.neednet=1
initrd ${osbase}/boot/initrd.img
boot
```

如果安装器要求根文件系统或网络安装源，应把厂商文档要求的参数追加到 `kernel` 行，例如：

```ipxe
kernel ${osbase}/boot/vmlinuz \
  initrd=initrd.img \
  ip=dhcp \
  root=/dev/ram0 \
  fetch=${osbase}/live/filesystem.squashfs
```

不要凭猜测添加 `root=`、`fetch=` 或 `inst.repo=`。参数错误通常会表现为内核启动后找不到根文件系统或安装源。

### 13.4 导入 custom OS

```bash
ipxe-manager add-os other-os \
  --kind custom \
  --source /root/ipxe-custom/other-os \
  --title 'Other OS 手动启动'
```

脚本会把目录复制到：

```text
/srv/ipxe/http/os/other-os/
```

并在各网口、IPv4/IPv6 菜单中加入：

```ipxe
chain ${base}/os/other-os/boot.ipxe
```

### 13.5 服务器端验证

```bash
ipxe-manager list-os
curl http://172.22.10.1:8080/os/other-os/boot.ipxe
curl -I http://172.22.10.1:8080/os/other-os/boot/vmlinuz
curl -I http://172.22.10.1:8080/os/other-os/boot/initrd.img
sudo python3 ipxe-verify.py
```

三个 HTTP 请求都应返回 `200 OK`。同时检查菜单：

```bash
grep -R -n -A5 -B2 'other-os' /srv/ipxe/http/menus/
```

### 13.6 客户端测试顺序

先测试 UEFI IPv4，再测试 UEFI IPv6；Legacy BIOS 需要确认网卡 Option ROM 能加载当前 `.pxe`。观察服务器日志：

```bash
journalctl -u ipxe-dhcp -f -o cat
tail -f /var/log/nginx/access.log
```

正常顺序应能看到：

```text
DHCP 请求
下载 boot.ipxe
下载 vmlinuz
下载 initrd
进入目标 OS 启动器
```

### 13.7 失败处理和回滚

如果菜单条目错误，先下架：

```bash
ipxe-manager disable-os other-os
```

修改源目录中的 `boot.ipxe` 后，重新导入前先移除旧条目：

```bash
ipxe-manager remove-os other-os
ipxe-manager add-os other-os \
  --kind custom \
  --source /root/ipxe-custom/other-os \
  --title 'Other OS 手动启动'
```

`remove-os` 会把旧目录移入回收目录。确认客户端不再下载旧文件后，再清理回收目录。不要直接修改 `/srv/ipxe/http/menus/*.ipxe`，因为刷新菜单会覆盖手工修改。

## 14. 非标准 OS 小白实操版

本节按“拿到一个不知道结构的 ISO”开始讲。示例假设 ISO 在 `/root/other-os.iso`，OS ID 使用 `other-os`。实际操作时把路径和 ID 替换成你的文件。

### 14.1 先确认文件路径

先确认 ISO 是否真的存在：

```bash
pwd
ls -lh /root/other-os.iso
file /root/other-os.iso
```

如果不确定文件名，列出 `/root` 下的 ISO：

```bash
find /root /mnt /opt -maxdepth 2 -type f \\
  \( -iname '*.iso' -o -iname '*.img' \) -printf '%p  %s bytes\\n' 2>/dev/null
```

文件名中有空格时必须加引号：

```bash
ls -lh '/root/My Other OS.iso'
```

### 14.2 查看 ISO 根目录

只查看，不会修改 ISO：

```bash
xorriso -indev /root/other-os.iso -ls / 2>/dev/null
```

常见结果和判断：

```text
.treeinfo + repodata + images/pxeboot  → RHEL 类完整 DVD
casper/vmlinuz + casper/initrd       → Ubuntu Live
EFI/BOOT + isolinux + images          → Boot ISO 或自定义启动盘
只有 EFI/BOOT/BOOTX64.EFI            → 可能只能走 EFI chain 启动
```

如果命令报 `Cannot open`，先检查路径、权限和文件是否完整：

```bash
ls -lh /root/other-os.iso
sha256sum /root/other-os.iso
```

### 14.3 查找内核和 initrd

先扫描文件名：

```bash
xorriso -indev /root/other-os.iso \\
  -find / -type f \\
  \( -iname 'vmlinuz*' -o -iname 'linux*' -o \\
     -iname 'initrd*' -o -iname 'initramfs*' -o \\
     -iname 'BOOTX64.EFI' \) -print 2>/dev/null
```

常见输出：

```text
/boot/vmlinuz
/boot/initrd.img
```

或：

```text
/install.amd/vmlinuz
/install.amd/initrd.gz
```

记录两个路径：

```text
ISO 内核路径：/boot/vmlinuz
ISO initrd 路径：/boot/initrd.img
```

### 14.4 查找原始启动参数

只找到文件名还不够，还要找到原 ISO 启动时使用的参数。检查 Legacy 配置：

```bash
xorriso -indev /root/other-os.iso \\
  -find / -type f \\
  \( -iname '*.cfg' -o -iname '*.conf' -o -iname '*.menu' \) -print 2>/dev/null
```

提取启动配置到临时目录：

```bash
rm -rf /tmp/other-os-iso
mkdir -p /tmp/other-os-iso
xorriso -osirrox on -indev /root/other-os.iso \\
  -extract /isolinux /tmp/other-os-iso/isolinux
xorriso -osirrox on -indev /root/other-os.iso \\
  -extract /boot /tmp/other-os-iso/boot
find /tmp/other-os-iso -type f -maxdepth 4 -print
```

搜索关键字：

```bash
grep -RniE 'linux|kernel|initrd|append|root=|fetch=|url=|repo=' \\
  /tmp/other-os-iso 2>/dev/null
```

如果 ISO 使用 GRUB，再查：

```bash
xorriso -indev /root/other-os.iso \\
  -find / -type f -iname 'grub.cfg' -print 2>/dev/null
```

找到 `linux`、`initrd` 或 `linuxefi` 行后，复制其参数。不要凭经验删除 `root=`、`fetch=`、`url=` 等参数。

### 14.5 创建工作目录

```bash
mkdir -p /root/ipxe-custom/other-os/boot
```

提取内核。下面的 ISO 路径必须替换成第 14.3 步查到的真实路径：

```bash
xorriso -osirrox on -indev /root/other-os.iso \\
  -extract /boot/vmlinuz \\
  /root/ipxe-custom/other-os/boot/vmlinuz
```

提取 initrd：

```bash
xorriso -osirrox on -indev /root/other-os.iso \\
  -extract /boot/initrd.img \\
  /root/ipxe-custom/other-os/boot/initrd.img
```

如果实际文件叫 `initrd.gz`，命令必须改成：

```bash
xorriso -osirrox on -indev /root/other-os.iso \\
  -extract /install.amd/initrd.gz \\
  /root/ipxe-custom/other-os/boot/initrd.gz
```

检查提取结果：

```bash
ls -lh /root/ipxe-custom/other-os/boot
file /root/ipxe-custom/other-os/boot/*
```

如果还需要 squashfs 或安装目录，必须保留相同的相对目录。例如 ISO 中有：

```text
/live/filesystem.squashfs
```

就提取到：

```bash
mkdir -p /root/ipxe-custom/other-os/live
xorriso -osirrox on -indev /root/other-os.iso \\
  -extract /live/filesystem.squashfs \\
  /root/ipxe-custom/other-os/live/filesystem.squashfs
```

### 14.6 创建 boot.ipxe

```bash
cat > /root/ipxe-custom/other-os/boot.ipxe <<'EOF'
#!ipxe
set osbase http://${next-server}:8080/os/other-os
kernel ${osbase}/boot/vmlinuz initrd=initrd.img ip=dhcp rd.neednet=1
initrd ${osbase}/boot/initrd.img
boot
EOF
```

检查第一行：

```bash
head -n 1 /root/ipxe-custom/other-os/boot.ipxe
```

必须显示：

```text
#!ipxe
```

### 14.7 IPv6 版本

如果客户端从 IPv6 菜单启动，复制一份 IPv6 文件：

```bash
cp /root/ipxe-custom/other-os/boot.ipxe \\
   /root/ipxe-custom/other-os/boot-ipv6.ipxe
sed -i 's/ip=dhcp/ip=dhcp6/' \\
  /root/ipxe-custom/other-os/boot-ipv6.ipxe
```

实际项目通常由菜单选择 IPv4/IPv6；如果同一 `boot.ipxe` 要兼容两者，应根据该 OS 的 initramfs 文档确认它是否支持 `ip=dhcp6`，不能只替换字符串。

### 14.8 导入到 ipxe-manager

先确认目录结构：

```bash
find /root/ipxe-custom/other-os -maxdepth 3 -type f -print
```

应该看到：

```text
/root/ipxe-custom/other-os/boot.ipxe
/root/ipxe-custom/other-os/boot/vmlinuz
/root/ipxe-custom/other-os/boot/initrd.img
```

执行导入：

```bash
ipxe-manager add-os other-os \\
  --kind custom \\
  --source /root/ipxe-custom/other-os \\
  --title 'Other OS 手动启动'
```

如果出现 `OS id already exists`：

```bash
ipxe-manager list-os
ipxe-manager remove-os other-os
```

确认旧文件已归档后，再重新执行 `add-os`。

### 14.9 验证发布文件

```bash
find /srv/ipxe/http/os/other-os -maxdepth 3 -type f -ls
curl -fsS http://172.22.10.1:8080/os/other-os/boot.ipxe
curl -I http://172.22.10.1:8080/os/other-os/boot/vmlinuz
curl -I http://172.22.10.1:8080/os/other-os/boot/initrd.img
```

三个请求都应成功。若 `404`，按实际目录检查：

```bash
find /srv/ipxe/http/os/other-os -type f
```

然后修正 `boot.ipxe` 中的路径，再重新导入。

### 14.10 验证菜单和客户端下载

```bash
grep -Rni -A6 -B2 'other-os' /srv/ipxe/http/menus/
sudo python3 ipxe-verify.py
```

客户端启动时同时观察：

```bash
journalctl -u ipxe-dhcp -f -o cat
tail -f /var/log/nginx/access.log
```

日志应按顺序出现：

```text
DHCPDISCOVER/DHCPREQUEST
下载 boot.ipxe
下载 vmlinuz
下载 initrd
```

只看到 DHCP、看不到 HTTP 请求，说明还没有进入 iPXE 菜单。能下载内核但启动后报找不到 root 或 squashfs，说明 `boot.ipxe` 的内核参数或文件相对路径不正确。

### 14.11 修改、下架和删除

只修改启动参数：

```bash
nano /root/ipxe-custom/other-os/boot.ipxe
ipxe-manager remove-os other-os
ipxe-manager add-os other-os --kind custom \\
  --source /root/ipxe-custom/other-os \\
  --title 'Other OS 手动启动'
```

临时下架但保留文件：

```bash
ipxe-manager disable-os other-os
```

恢复：

```bash
ipxe-manager enable-os other-os
```

查看归档位置：

```bash
find /var/lib/ipxe-manager/trash -maxdepth 3 -type f -o -type d
```

不要直接编辑 `/srv/ipxe/http/menus/*.ipxe`，因为 `refresh-menu` 会重新生成并覆盖它们。
