# CoLink 的 ChatGPT 插件与 Skill 包

这个目录保存维护者现有私有插件的展示资源及本机打包说明。公开文件只有图标和
说明；个人 `.app.json`、`plugin.json`、`.codex-plugin/` 被 Git 忽略，保留在本机，
不属于可分发插件模板，也不上传到 GitHub。其他用户不要复用维护者的连接映射，
应按 [安装指南](../../docs/INSTALL.md) 自行创建私有连接。

## 本机新版

ChatGPT 上传包版本为 `1.0.4`，与 macOS 应用版本独立。保留现有插件技术身份及
`.app.json` 连接映射，通过原插件的“上传新版本”更新。

展示名称为 CoLink。说明改为代码智能：按需读取真实源码，理解项目结构、调用关系、
数据传播和改动影响，辅助分析、编写、修复、重构及验证代码。Skill 不列具体编程
语言或固定覆盖清单，以当前实际工具的 schema 和返回的覆盖情况选择查询。
写入、终端及数据库操作仍由用户在本机授权，安装包不能自行开启权限。

图标使用用户提供的 256 × 256 PNG。详情页及聊天输入框的深浅主题图标均引用
`assets/logo.png`；`assets/logo.svg` 是引用同一 PNG 的展示包装。这两份资源独立于
桌面应用图标，本次无需更换 macOS 安装版。

## 包内容

完整插件 ZIP 的根目录包含：

```text
plugin.json
.app.json
.codex-plugin/plugin.json
assets/logo.png
assets/logo.svg
skills/colink-code-context/
  SKILL.md
  agents/openai.yaml
  assets/logo.png
  references/code-intelligence.md
  references/write-and-run.md
```

根 `plugin.json` 使用 portable 格式，`skills/` 位于固定发现目录；OpenAI 展示和
连接映射位于 `extensions.com.openai`。兼容 manifest 保留相同身份、文案和映射，
并声明 `skills: "./skills/"`。所有资源随包携带，不依赖仓库中的外部文件。

Skill 的唯一维护来源是
[`integrations/skills/colink-code-context`](../skills/colink-code-context/SKILL.md)。
打包时从这里复制完整目录，不维护第二份正文。独立 Skill ZIP 仅包含此目录及资源，
不含个人插件 manifest，适合单独更新已有 Skill。安装入口与证据边界见
[Skill 安装指南](../../docs/CHATGPT_SKILL.md)。

本机输出位于 `dist/chatgpt-plugin/`：

- `code-connect-1.0.4.zip`：更新现有私有插件，含图标、说明和捆绑 Skill。
- `colink-code-context-1.0.4.zip`：独立 Skill，含主文档、两个参考文件、UI 声明和图标。

只打包上述明确文件；不包含项目源码、`.env.local`、`.code-context`、依赖或运行数据。
本机归档校验不等于 ChatGPT 网页已上传、导入或自动匹配成功。仓库修改也不会自动
更新账号中已安装的副本。GitHub 发布不是公共插件目录发布。

用户已明确要求新版通过校验后移走本目录对应的旧上传包 `1.0.1`、`1.0.2`、`1.0.3`，
只保留新版上传 ZIP；优先移入废纸篓。其他历史验收归档、恢复资料和 macOS 安装包
不属于此次清理范围。
