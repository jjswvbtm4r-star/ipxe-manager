# iPXE 双栈装机服务

部署器使用 Python 3.10+ 编写，只依赖 Python 标准库；不需要 Bash 管理脚本、`jq`、`sipcalc`、`grepcidr` 或 `pip`。程序负责配置校验、配置生成、iPXE 编译、OS 导入、菜单维护、快照和失败回滚。dnsmasq 提供 DHCPv4、DHCPv6、RA、TFTP；nginx 提供 HTTP 镜像服务。自动配置网卡地址的部署目标为 Ubuntu Server 22.04/24.04 和 systemd-networkd。

## 一键部署

解压 `ipxe-manager.zip` 后进入目录：

```console
unzip -q ipxe-manager.zip -d /tmp
cd /tmp/ipxe-manager
sudo python3 install.py
```

系统要求 Python 3.10 或更新版本。首次安装需要 Ubuntu/Debian 软件源和 GitHub 连通性；安装程序会安装系统依赖、构建固定版本的 iPXE 并部署服务。服务器已存在 `/etc/ipxe-manager/config.json` 时会保留原配置；首次安装才复制 `config.example.json`。运行中环境先备份配置，再确认 `config.example.json` 中的网口、地址和地址池后部署。

若系统依赖已预装，可跳过 apt 安装：

```console
sudo python3 install.py --skip-packages
```

之后管理器命令由 `/usr/local/sbin/ipxe-manager` 指向 Python 程序：

```console
sudo ipxe-manager status
sudo ipxe-manager --help
```

服务器上修改 `/etc/ipxe-manager/config.json` 后重新应用：

```console
sudo ipxe-manager deploy
```

## 本机网络方案示例

| 网口 | IPv4 | IPv6 | 用途 |
|---|---|---|---|
| enp90s0f0 | 172.22.10.1/24 | fd42:19:10::1/64 | 独立装机网络 1 |
| enp90s0f1 | 172.22.11.1/24 | fd42:19:11::1/64 | 独立装机网络 2 |
| enx00e04c580a4c | 原有管理网络 | 原有配置 | 保留管理网络，不提供 DHCP/RA |

每个装机口应连接独立 VLAN/二层网络，避免客户端收到多套 DHCP 配置。默认不向装机客户端提供网关或 DNS。程序会拒绝使用承载默认路由或当前 SSH 连接的网卡，也会检查子网重叠和已有 DHCP 服务。

`config.example.json` 可按实际网口增删 `networks[]` 条目。配置了 `configure_address: false` 时，程序不创建该网口的 `.network` 文件，但会确认所需静态地址已由其他网络管理工具配置。

## 引导支持范围

| 模式 | 支持情况 |
|---|---|
| x86 Legacy BIOS + IPv4 PXE | 原生驱动 iPXE `.pxe` -> HTTP 菜单 |
| x86-64 UEFI + IPv4 PXE | 固件 SNP -> iPXE -> HTTP 菜单 |
| x86-64 UEFI + IPv6 PXE | DHCPv6/RA/TFTP -> iPXE -> IPv6 HTTP 菜单 |
| UEFI HTTP Boot | 固件支持时由 DHCP 下发 HTTP 引导 URL |
| Legacy BIOS 原生 IPv6 PXE | 不支持，传统 BIOS PXE 没有通用 IPv6 启动标准 |
| Secure Boot | 自编译 iPXE 未签名，默认需关闭 Secure Boot |
| IA32 UEFI、ARM64 UEFI | 当前未编译 |

若 Legacy 客户端在 DHCP 前报 `PXE-EC8: !PXE structure was not found`，故障位于客户端 Option ROM 阶段；这是客户端固件/网卡初始化问题，不是 OS 菜单配置。看到本项目菜单表示第一阶段文件、DHCP、iPXE 和 HTTP 菜单链路已经打通。

## OS 导入与维护

Ubuntu Live Server ISO：

```console
sudo ipxe-manager add-os ubuntu-24-04 --kind ubuntu-live \
  --source /root/ubuntu-24.04-live-server-amd64.iso --title 'Ubuntu 24.04 Server'
```

Rocky、AlmaLinux、CentOS Stream、RHEL 兼容的完整 DVD ISO：

```console
sudo ipxe-manager add-os rocky-9 --kind rhel \
  --source /root/Rocky-9-x86_64-dvd.iso --title 'Rocky Linux 9'
```

`rhel` 类型要求安装树中有 `.treeinfo`、内核、initrd 和本地仓库元数据。仅含网络引导程序的 boot ISO 不可按 DVD 类型导入。自定义 OS 使用管理员维护的 `boot.ipxe`：

```console
sudo ipxe-manager add-os rescue --kind custom \
  --source /srv/custom/rescue --title 'Rescue tools'
```

常用维护：

```console
sudo ipxe-manager list-os
sudo ipxe-manager disable-os rocky-9
sudo ipxe-manager enable-os rocky-9
sudo ipxe-manager refresh-menu
sudo ipxe-manager remove-os rocky-9
sudo ipxe-manager rollback /var/lib/ipxe-manager/backups/时间戳
```

禁用会从菜单隐藏 OS，但保留文件。删除会把配置和文件移到 `/var/lib/ipxe-manager/trash/`，便于恢复。确认不再有安装任务后才清理回收区。

## 管理器命令

| 命令 | 用途 |
|---|---|
| `deploy` | 部署/更新服务，失败时恢复本次部署前快照 |
| `status` | 查看服务、接口及 HTTP 菜单 |
| `render --output DIR` | 只渲染配置，用于部署前检查 |
| `list-os` | 列出 OS 配置 |
| `add-os` | 导入 ISO 或自定义 OS 文件夹 |
| `enable-os` / `disable-os` | 控制 OS 是否显示在菜单 |
| `remove-os` | 从菜单移除并归档文件 |
| `refresh-menu` | 按现有 OS 配置重新生成菜单 |
| `rollback BACKUP` | 恢复某个部署快照 |

## 重要路径

| 路径 | 内容 |
|---|---|
| `/opt/ipxe-manager/ipxe-manager.py` | Python 3 管理程序 |
| `/etc/ipxe-manager/config.json` | 网络与 iPXE 版本配置 |
| `/etc/ipxe-manager/os.d/` | 每个 OS 的独立 JSON 配置 |
| `/srv/ipxe/http/os/` | OS 安装文件 |
| `/srv/ipxe/http/boot/` | BIOS/UEFI 引导文件 |
| `/srv/ipxe/http/menus/` | 按网口、地址族生成的菜单 |
| `/var/lib/ipxe-manager/backups/` | 部署前快照 |
| `/var/lib/ipxe-manager/trash/` | 已移除 OS 的回收区 |
| `/var/lib/ipxe-manager/leases` | DHCP 租约 |

v1.2.0 提供 Python 3 管理器和安装包。服务器实际部署及 DHCP/TFTP/HTTP、Legacy/UEFI 客户端启动尚未由 Python 版验证；请先在隔离装机网络完成验收。
