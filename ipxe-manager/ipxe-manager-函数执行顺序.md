# ipxe-manager.py 执行流程

本说明按 Python 3 管理程序的执行顺序，帮助维护人员定位配置校验、部署、菜单和 OS 导入逻辑。

## 1. 所有命令经过的入口

```text
main()
├─ build_parser()       解析 --config 和子命令参数
├─ read_config()        读取 JSON 并调用 validate_config()
├─ root 检查            render 只渲染文件，其余操作需要 root
└─ fcntl.flock          防止两个管理操作同时改配置
```

## 2. deploy 部署链路

```text
main()
└─ deploy_all()
   ├─ check_host()          检查发行版、管理网口、地址重叠和 DHCP 冲突
   ├─ install_dependencies()安装 dnsmasq、nginx、iPXE 编译工具
   ├─ make_backup()         保存配置、服务状态、boot 和菜单
   ├─ build_ipxe()          下载/切换固定源码并编译启动文件
   │  ├─ base_url()         生成 IPv4/IPv6 HTTP 地址
   │  ├─ boot_name()        生成 BIOS/UEFI 文件名
   │  └─ run([...])         用参数数组调用 git、make，不经过 shell
   ├─ render_configs()      生成 dnsmasq、nginx 和 networkd 配置
   ├─ dnsmasq --test        检查 DHCP/TFTP 配置
   ├─ nginx -t              检查 HTTP 配置
   ├─ write_units()         写入独立 systemd 服务
   ├─ networkctl            仅重新加载/配置 iPXE 管理的装机网口
   ├─ render_menus()        生成各网口的 IPv4/IPv6 菜单
   ├─ systemctl              启用并重启 DHCP、HTTP 服务
   └─ status_check()         检查服务和菜单 HTTP 响应
```

任一步失败时，`deploy_all()` 调用 `restore_backup()`，恢复部署前文件、服务状态和启动菜单。包管理器安装的系统软件不会自动卸载。

## 3. OS 管理链路

```text
add-os
└─ add_os()
   ├─ valid_id()             检查唯一 ID
   ├─ safe_text()            检查标题和内核参数
   ├─ sha256_file()          计算 ISO SHA256
   ├─ run([... xorriso ...]) 提取启动文件/安装树
   ├─ 写入 /etc/ipxe-manager/os.d/<id>.json
   └─ render_menus()         生成客户端菜单和 catalog.json

remove-os
└─ remove_os()
   ├─ 把 JSON 移入 /var/lib/ipxe-manager/trash
   ├─ render_menus()         从菜单移除该 OS
   └─ 把 OS 文件移动到 trash/data
```

## 4. 其他命令

| 命令 | 主要函数 | 用途 |
|---|---|---|
| `status` | `status_check()` | 查看服务、网口和菜单健康状态 |
| `render` | `render_configs()` | 只生成配置，不写正式系统路径 |
| `list-os` | `list_os()` | 输出 OS 配置 |
| `refresh-menu` | `render_menus()` | 按现有配置重建菜单 |
| `enable-os` / `disable-os` | `set_os_enabled()` | 控制 OS 是否显示 |
| `rollback` | `restore_backup()` | 恢复所选部署快照 |
| `--help` | `build_parser()` | 输出命令和参数帮助 |

Python 标准库负责 JSON、IP/CIDR 运算、SHA256、文件备份、锁和参数解析。`systemctl`、`networkctl`、`ip`、`dnsmasq`、`nginx`、`xorriso`、`git` 和 `make` 仍负责各自的 Linux 系统工作；Python 通过 `subprocess` 参数列表调用它们，不拼接 shell 命令。
