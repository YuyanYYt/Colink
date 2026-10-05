# Colink 的 ChatGPT 图标包

开源用户不应上传或复用维护者的个人连接元数据。这个目录公开的只有品牌 SVG/PNG
和说明；已有个人 `.app.json`、`plugin.json`、`.codex-plugin/` 被 Git 忽略，保留在本机，
不属于可分发插件模板，也不上传到 GitHub。图标的网页显示问题未在本轮重新处理。

普通用户按 [安装指南](../../docs/INSTALL.md) 自行创建私有连接并命名为 Colink。
GitHub 发布不是 ChatGPT 公共插件目录发布，私有 Tunnel 也不能代替公共目录审核。

通用代码问答 Skill 已独立放在
[`integrations/skills/colink-code-context`](../skills/colink-code-context/SKILL.md)，
不包含此目录被忽略的个人连接文件。可通过 ChatGPT 技能编辑器或本机上传安装，
自动匹配规则与实际入口见 [Skill 安装指南](../../docs/CHATGPT_SKILL.md)。
独立安装 Skill 不等于已向现有私有 MCP 插件上传捆绑技能，也不修改其连接权限。

以下是维护者本机既有图标包的历史维护说明，**不是新用户安装步骤**：

这个目录仅保存现有私有插件的展示元数据，不运行代码，不包含源码、密钥或隧道凭据。
`.app.json` 沿用现有应用 ID；`plugin.json` 使用标准根目录格式，兼容 manifest
`.codex-plugin/plugin.json` 保留相同的连接映射与展示文案。
网页版插件包版本与 macOS 应用版本独立，当前图标包版本为 `1.0.3`。

`assets/logo.svg` 直接来自 `macos/assets/logo.svg`；`assets/logo.png` 使用同版桌面应用
已有的 256 × 256 导出结果。详情页 logo 和聊天输入框图标的深浅主题声明都指向这一 PNG。
更新桌面标志后，应同时更新这两份资产再打包；不需要重新创建 MCP 连接。

仅把 `plugin.json`、`.app.json`、`.codex-plugin/plugin.json`、`assets/logo.svg` 和 `assets/logo.png`
打包成 ZIP，通过原插件的“上传新版本”上传。不要打包项目根目录、`.env.local`、
`.code-context`、所选代码目录或依赖。

上传包和下载的原始包作为恢复证据保留，不自动清理。
