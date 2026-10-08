# CoLink 0.5.0b5 build 17 历史软件恢复锚点（已撤销）

2026-10-08 后续更新：用户已改选 build 19 并删除旧 CoLink 恢复标签。
当前入口见 [build 19 恢复说明](RECOVERY_0.5.0b5_BUILD19.md)。下文保留历史身份及证据，
其中旧标签查询和恢复指令不再是当前操作入口。

日期：2026-10-08（Asia/Shanghai）。本文件保留 build 17 原安装归档的身份和证据。
用户后续明确将首选源码恢复基准固定为 `9150904`，见
[9150904 恢复说明](RECOVERY_9150904.md)。本文件的历史标签不移动。

## 固定身份

- 固定 Git 标签：`anchor/colink-0.5.0b5-build17-terminal`，目标为包含本文的版本提交，标签不移动。
- 分支：`codex/colink-controlled-write`。
- 安装身份：`/Applications/Colink.app`；`CoLinkVersion=0.5.0b5`；`CFBundleVersion=17`。
- bundle ID：`local.codeconnect.menubar`；macOS / Apple Silicon。
- 源码提交完整 SHA 通过 `git rev-parse 'anchor/colink-0.5.0b5-build17-terminal^{commit}'` 查询；本机锚点记录也保存该值。
- 此锚点优先于历史 `anchor/colink-0.5.0b2-before-terminal-execution`。
  既有 0.4.1、0.4.3、0.5.0b2、0.5.0b4 锚点保留原目标。

本版包含只读 / 写入 / 开发三档、隐藏的存储设置、终端式数据库访问、持续终端输入与输出，
移除了数据库连接页和聊天数据库卡片。开发终端按当前 macOS 用户权限运行；只读与写入档
提供固定数据库读取命令。详细边界见 SECURITY.md 与 docs/TERMINAL_BUILD17_REVIEW.md。

## 核对状态

本版已完成静态检查、Swift 编译、自包含包构建、严格签名、重定位导入和本机安装核对。
保存锚点前再次确认：安装版本/build 正确，81 个后端源码/资源文件与工作区完全一致，
现有 ZIP/DMG 的 SHA-256 与打包记录一致。

按用户要求，实际终端、PTY、数据库认证/CRUD、撤权及网页 UI 测试由用户执行。
因此这是用户选定的软件恢复点，不将其称为实际功能全部验收通过或没有 Bug 的版本。

此版本提交不包含用户样例项目、临时数据库验收项目、凭据、运行数据库、应用运行状态，
也不包含原有 `docs/WRITE_CONTRACT.md` 的独立工作区改动。它们保留在本机，不因保存锚点重置。

## 离线恢复材料

Git bundle 与本机锚点记录：

```text
.artifacts/anchors/colink-0.5.0b5-build17-terminal/
├── CoLink-0.5.0b5-build17-source.bundle
├── RECOVERY.md
├── anchor.json
└── SHA256SUMS
```

复用本次已经构建的安装归档，不再复制一套应用：

```text
macos/dist/0.5.0b5-terminal-build17-final-20261008/Colink-macos-arm64.zip
macos/dist/0.5.0b5-terminal-build17-final-20261008/Colink-macos-arm64.dmg
```

归档校验值：

```text
326405b858a611c46be0e479912fa72c7ab7aaf3a3ab8324e7a30d0b99fe7474  Colink-macos-arm64.zip
5c60a01263e3dfe8bb0b0491f1d72776a28a93efb1720775e628f91eb18c9b8a  Colink-macos-arm64.dmg
```

上述 bundle、记录及引用的 ZIP/DMG 属于本锚点的保护材料，不应按旧构建或无用缓存清理。
如果确需移动，先更新本地引用并重新核对校验值；生成物不进入源码 Git 提交。

## 今后的恢复顺序

1. 出现需要恢复的软件回归时，优先评估本锚点。先保存当前源码、未提交改动、必要日志和
   未完成操作记录，正常停止故障应用及其作业。
2. 核对标签目标，从固定标签建立独立的 `codex/` 恢复分支或 checkout；工作区有改动时
   不使用强制 checkout 或 hard reset 覆盖。可用 `git bundle verify` 核对离线源码包。
3. 需要恢复安装版时先验证 ZIP/DMG 校验值，再从归档替换 `/Applications/Colink.app`。
   核对身份、版本、build、签名和基础启动，保留替换前应用直至恢复成功。
4. 先确认运行数据兼容性。后续版本改变 schema 或状态格式时，按实际迁移路径处理；
   不通过清空数据库、删除配置或重置目录选择绕过兼容问题。
5. 保留用户项目、目录选择、连接配置、密钥、数据库、依赖锁和未完成写入恢复记录。
   恢复后按本版默认关闭权限的启动语义运行，不自动开启开发档。
6. 本机恢复与网页 MCP 工具恢复分别检查。旧数据库卡片和工具已移除，网页客户端可能需要
   更新工具清单；不能仅凭本机启动成功宣称网页恢复完成。

软件锚点不能撤销已完成的数据库操作、终端文件修改、依赖安装或外部网络副作用。
用户项目或数据库需要恢复时，应按各自的 Git/备份记录和明确授权另行处理。
