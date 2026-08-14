# GitHub 上传说明

本目录已经整理为单一 Git 仓库根目录，公开仓库名使用 `codex-deepseek-relay`；内部 Skill 目录继续使用 `codex-deepseek-subagent`，以保持安装兼容。

## 已完成的准备

- 完整源码、Skill、说明文档、测试和 GitHub Actions 均位于本目录；
- 已排除 Python 缓存、旧源码备份、压缩备份、日志、本地环境和凭据文件；
- Windows 启动脚本只使用 `CODEX_HOME` 或 `%USERPROFILE%\.codex`，不包含作者机器的绝对路径；
- 默认分支为 `main`，本地首个提交已准备完成；
- README 中的安装地址按本机 Git 身份设置为 `Roblis0n/codex-deepseek-relay`；
- API Key 不会进入 Git，运行时只写入 Windows Credential Manager 或 macOS Keychain。

## 上传步骤

先在 GitHub 创建一个名为 `codex-deepseek-relay` 的空仓库，不要勾选自动创建 README、LICENSE 或 `.gitignore`。然后在本目录执行：

```powershell
git remote add origin https://github.com/Roblis0n/codex-deepseek-relay.git
git push -u origin main
```

如果实际 GitHub 账号不是 `Roblis0n`，只需把上述远程地址和 README 中的账号名替换为实际账号；源码、测试和提交无需重做。

## 上传后核对

1. GitHub Actions 的 Windows 与 macOS 两个测试任务均通过；
2. README 顶部图片、工作流图和本地文档链接正常显示；
3. 仓库中不存在 `.env`、`auth.json`、API Key、日志、缓存、备份或压缩包；
4. 仓库根目录直接包含 `README.md`、`LICENSE`、`.github`、`codex-deepseek-subagent` 和 `scripts`，没有多余的外层目录。
