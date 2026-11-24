# Changelog

## v1.2.0 — 2026-11-24

- 将部署管理器改为 Python 3.10+，使用标准库处理配置、双栈地址校验、菜单、ISO、备份和回滚。
- 将安装入口、只读验收脚本、软件包构建工具和本地发布工具改为 Python。
- 移除 Bash 部署器及对 `jq`、`sipcalc`、`grepcidr` 的运行依赖；现有 `ipxe-manager` 子命令和 JSON 配置保持兼容。
- 重建管理器、一键部署和 SOP 三个 ZIP 包，并在 ZIP 中保留 Python 入口文件的 Unix 可执行权限。
- Python 版尚未部署到现场服务器；DHCP/TFTP/HTTP 与客户端 PXE 实测待后续验证。

## v1.1.0 — 2026-11-23

- 修复 Ubuntu 上 `sipcalc` 输出使用制表符时地址解析失败的问题。
- 修复 `enabled: false` 和 `configure_address: false` 被当作默认值的问题，禁用的 OS 不再进入菜单。
- 兼容由 NetworkManager 等工具预先配置地址的环境，避免在不需要时调用 `networkctl`；已有 networkd 配置需要变更时仍检查服务状态。
- 修复主机检查通过但因最后一项服务未运行而提前退出部署的问题。
- 增加隔离回归测试 `tests/regression.sh`，覆盖地址解析、布尔值及冲突服务检查。
- 已在 Ubuntu 服务器验证服务、双网口 IPv4/IPv6 菜单和启动文件传输；openEuler 24.03 LTS SP1 已导入，PXE 启动由现场验证。
- 增加 Other OS、Boot ISO 和 custom `boot.ipxe` SOP。
- 增加 `ipxe-verify.sh` 服务器端只读验收脚本。
- 扩展 `ipxe-manager --help` 和 `add-os --help`。

## v1.0.0

- 初始 PXE/iPXE 双栈部署版本。
