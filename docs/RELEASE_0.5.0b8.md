# CoLink 0.5.0 Beta 8 · build 39

2026-10-29。基于当前 `main` 发布的 macOS 安装包更新。`v0.5.0b7` 保留为此前固定的源码发行；本版使用新标签 `v0.5.0b8`，不移动或覆盖历史标签。

## 本版内容

- 将公开 macOS 安装包从 `0.5.0b5 / build 22` 更新至当前源码的 `0.5.0b8 / build 39`。
- 安装包以 Live 模式运行，包含 Python、锁定的 MCP 依赖、项目源码和官方隧道客户端。
- 包含 b7 之后合入的读取预算与符号定位修复、分析请求路由和响应边界改进，以及 TypeScript / JavaScript 结构关系能力；Python / Java 仍是值传播支持范围。
- 同步发布当前 CoLink Skill 备用 ZIP 和 SHA-256 校验清单。

## 下载与平台

- `Colink-macos-arm64.dmg`：推荐的拖放安装包。
- `Colink-macos-arm64.zip`：备用应用归档。
- `colink-code-context-skill.zip`：可选的调用指导，不会自动配置连接或授权。
- `SHA256SUMS`：校验三个归档文件。下载后在同一目录执行 `shasum -a 256 -c SHA256SUMS`。

本次归档的 SHA-256：

- DMG：`adb45ae7a1e3100138492afefb8c4cb53e78d247578bd4bac1fbf2faa507849f`
- ZIP：`56bcd49a72d0de9e7cfeb7262d9a4a027beef39d380ea97438405f660e2ef98c`
- Skill ZIP：`5ab1edd456e4a91e917a652eb1a73d937ac80061fc930b6a6e8bba9e7f2caf1c`

应用面向 Apple Silicon Mac，最低要求 macOS 14。Intel Mac 和 Windows 没有图形安装包；可在 macOS / Linux 使用源码 CLI。发行版采用 ad-hoc 签名，尚未进行 Apple Developer ID 签名或公证，macOS 可能显示安全提示。

## 更新说明

先正常关闭并退出旧版，再替换应用。更新保留 `~/Library/Application Support/Colink/` 中的设置、凭据和持久数据；首次打开后连接保持关闭，不自动扩大目录、写入或开发授权。旧的 GitHub Release 和恢复材料继续保留。

CoLink 的私有隧道与项目选择由每位用户自行配置。发布安装包不代表已接入 Accio Work，也不提供公开 SaaS 服务。

## 验证

在 Apple Silicon macOS 上生成自包含 Live 包，包报告确认版本 `0.5.0b8`、build `39`、最低 macOS `14.0` 和 Live 模式。重定位后的 Python / MCP 导入检查、应用严格签名校验、DMG 校验、两个 ZIP 的归档检查及三项 SHA-256 校验通过。发行扫描覆盖 346 个源码文件，0 项发现。源码 CI 与自包含应用包检查的覆盖范围不同；均不等同于任意账号的网页 MCP 调用验收。
