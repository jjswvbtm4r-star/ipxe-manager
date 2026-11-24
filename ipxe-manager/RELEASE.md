# iPXE Manager Release Guide

## Repository files

- `ipxe-manager.py`: Python 3 管理器
- `install.py`: 安装程序并启动首次部署
- `config.example.json`: 配置示例
- `MANUAL_DEPLOYMENT_TRAINING.md`: 内部部署 SOP
- `ipxe-verify.py`: 服务器端只读验收工具
- `packages/`: 本地生成的部署和 SOP ZIP 包

ISO 文件、SSH 密钥、服务器运行目录和临时构建内容不进入 Git。

## 版本规则

当前版本为 `v1.2.0`。`v1.0.0` 和 `v1.1.0` 保留历史。后续日常修改只留在工作区；创建新标签和 Release 需由用户明确要求。

## 准备发布

确认用户同意发布后，先更新 `CHANGELOG.md` 和 `VERIFICATION.md`，再检查工作区并运行：

```console
python3 tools/release.py 1.2.0
```

该脚本会重建 ZIP、提交本地 Git 变更并创建带注释的版本标签。它不会推送到 Forgejo，也不会创建 Forgejo Release。远程推送和网页 Release 需要按用户明确指示执行。
