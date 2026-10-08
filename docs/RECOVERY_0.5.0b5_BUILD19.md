# 当前首选恢复基准：CoLink 0.5.0b5 / build 19

2026-10-08（Asia/Shanghai），用户在安装及索引验收后明确要求删除旧锚点，
以后优先恢复本机这一版。本指令替代此前首选 9150904 和保留旧恢复标签的要求。

## 固定身份

- 首选 Git 标签：`anchor/colink-0.5.0b5-build19-verified`。
- 安装版源码：`79ad2aecf9472e669dc46254dea133185c11e01a`。
- 标签包含上述完整实现及本次恢复说明更新，应用源码与该提交相同。
- 本机应用：`/Applications/Colink.app`，`CoLinkVersion=0.5.0b5`，`CFBundleVersion=19`。
- bundle ID：`local.codeconnect.menubar`，Apple Silicon / macOS。
- 新锚点建立前已再次核对严格签名、应用身份和 83 个后端源码/资源一致。

本版已完成 2,585 项全量回归、Ruff 与格式检查、5,000 文件基准及安装版实际 MCP
九项结构查询。四个既有内容防护排除文件仍报告 `index_partial`，不是容量错误。
验收边界与原始报告见 [索引修复记录](LIVE_INDEX_RUNTIME_FIX_20261008.md)。
该记录不宣称全部终端、业务数据库或未来规模均无 Bug。

## 恢复材料

离线源码和本机记录：

```text
.artifacts/anchors/colink-0.5.0b5-build19-verified/
├── CoLink-0.5.0b5-build19-source.bundle
├── RECOVERY.md
├── anchor.json
├── before-anchor-update.json
└── SHA256SUMS
```

复用已验证的安装归档：

```text
macos/dist/0.5.0b5-index-runtime-build19-20261008/Colink-macos-arm64.zip
macos/dist/0.5.0b5-index-runtime-build19-20261008/Colink-macos-arm64.dmg
```

校验值：

```text
7a8ce814ed3d0f09268e843c6e3f25b1a76ce315d6d2f58db5eeb55f0a69a233  Colink-macos-arm64.zip
d4366b1b0c2f5a0aeffcf68b5442159a436850ad2cd7e536d93a9b9b0ab16909  Colink-macos-arm64.dmg
```

新记录、源码 bundle 和引用的 ZIP/DMG 是当前恢复材料，后续清理前应核对引用。
不重复复制一份应用，不将 `.artifacts`、私人配置或运行数据库提交到源码仓库。

## 恢复步骤

1. 保存当前提交、未提交补丁、未跟踪用户项目及必要日志，正常停止故障连接和作业。
2. 核对标签指向及 `anchor.json` 中的完整提交，可用 `git bundle verify` 验证离线源码。
   从标签建立独立的 `codex/` 恢复分支或 checkout，不覆盖脏工作区。
3. 需要恢复安装版时先核对归档 SHA-256；保留替换前安装状态，再恢复固定身份。
   检查版本、签名、基本启动与实际 MCP，失败时保留原安装和诊断。
4. 保留项目登记、目录选择、连接配置、凭据、业务数据库、依赖锁及未完成写入恢复记录。
   不通过清空 `.code-context` 或重置用户设置绕过迁移问题。
5. 本版每个项目单独保存当前索引，路径为
   `live-v1/index/projects/<项目ID哈希>/live-index.sqlite3`，每库磁盘上限 500 MiB，
   最多同时打开 4 个连接。索引重建前仍检查身份与兼容性。
6. 恢复不会自动开启写入/开发档、扩大目录权限、设置自启或撤销业务数据库外部操作。

## 旧锚点状态

以下六个本地 Git 恢复标签已按用户指令删除：

- `anchor/colink-0.4.1-before-refactor`
- `anchor/colink-0.4.3-before-live-workspace-refactor`
- `anchor/colink-0.5.0b2-before-terminal-execution`
- `anchor/colink-0.5.0b4-before-database-ui`
- `anchor/colink-0.5.0b5-build17-terminal`
- `anchor/colink-before-live-index-optimization`

旧记录作为历史证据存在，不再是活动恢复入口。旧材料中有未提交源码及私人配置备份，
不因撤销标签而销毁。Git 历史、发行标签、原始源码、配置、凭据和数据库保留。
