# Colink · ChatGPT 自动匹配 Skill

核对日期：2026-10-05。目标是用户询问自己的代码、缺少必要源码时优先使用 Colink；
已有充分代码片段时直接解释，不为了工具调用而重复读取。

## 当前网页端的真实入口

在维护者实际的 ChatGPT Plus 账号中，已看到并进入以下界面：

**侧边栏「插件」 → 「技能」 → 「添加技能」 → 「使用编辑器创建」**。

「添加技能」还列出「通过聊天创建」和「从电脑上传」。技能编辑器包含名称、描述和
`SKILL.md` 正文输入区；不必把这些规则塞进项目指令。入口、工作区权限及开放情况
可能不同，不能把一个账号看到入口推断为所有账号均有，也不能继续宣称 Plus 一定没有。

## 文件及使用方法

- 核心文件：[SKILL.md](../integrations/skills/colink-code-context/SKILL.md)。
- 可选 UI/自动调用声明：[agents/openai.yaml](../integrations/skills/colink-code-context/agents/openai.yaml)。
- 保持已有 Colink 连接可用；Skill 本身不启动应用、不创建隧道、不配置密钥。

公共 Skill 不写维护者的私有 MCP URL/依赖。它只能使用当前对话已经可用的 Colink
工具，不会自动授权或开启一个未启用的连接；“只安装这个文件就能自动带入所有
MCP 工具”不是已验证能力。后续网页验收需同时确认技能与原 Colink 工具在该对话可用。

编辑器方式：

1. 名称填写 `colink-code-context`。
2. 描述使用 `SKILL.md` 顶部的 `description` 值，正文使用 frontmatter 之后的内容。
3. 创建为自己使用的 Skill，检查安装/启用状态。无需分享到其他工作区。
4. 在新对话自然提问，例如「看看我已连接的本机项目有哪些文件，只读清单即可」。
   不手动 `@` 选择技能，检查是否出现技能使用和 Colink 工具调用。
5. 再用已贴出充分代码、一般语法、外部教程等反例检查不会无关读取。

本轮另提供只含 `SKILL.md` 和 `agents/openai.yaml` 的独立 ZIP 备用包。网页确有
「从电脑上传」入口，但其接受的归档格式及创建结果尚未实测，以实际界面提示为准；
推荐先用已观察到字段的编辑器方式。不要上传项目根目录、所选源码、`.env.local`、
`.code-context` 或个人插件 manifest。导入后仍需确认技能已启用。

## 自动匹配不是强制系统提示词

官方说明：启用的技能可以根据请求内容隐式选择；`name` 和 `description` 帮助模型
判断何时加载正文。显式 `@` 只是另一种调用方式，不是每次使用的必需条件。
见 [ChatGPT Skills 与 Plugins](https://learn.chatgpt.com/docs/skills-and-plugins)、
[Skill 概念](https://developers.openai.com/plugins/concepts/skills)。

`agents/openai.yaml` 显式设置 `policy.allow_implicit_invocation: true`；官方说明
隐式调用默认开启。仍由当前客户端、对话和可用工具决定是否加载，不能保证每次命中，
更不能强制成为高优先级 system prompt。
见 [创建 ChatGPT Skills](https://learn.chatgpt.com/docs/build-skills)。

该官方页面还区分：独立技能用于桌面应用/CLI/IDE，捆绑到 Plugin 的技能用于网页等
Chat/Work 环境。这里的网页账号虽然实际有技能编辑器，仍需要完成创建后观察它的
分发形式、安装与当前聊天可用状态；不能仅凭编辑入口断言独立 Skill 已能在网页自动跑。
如果网页创建路径要求捆绑 Plugin，再按界面与官方结构处理，不把 Codex 本机安装
冒充 ChatGPT 网页安装。

规则分层：Skill 负责调用前判断代码归属和上下文是否足够；服务器 instructions 负责
固定快照、只读与错误恢复；单工具说明负责参数、分页和响应预算。三者不替代用户授权。

## 本机文件与网页不是自动同步的

修改仓库里的 `SKILL.md` 不会自行更新网页已安装副本。后续修改需在网页编辑/更新，
或重新上传技能包。Codex 的 `.agents/skills` 扫描规则不代表 ChatGPT 会读取本机目录。

将来若捆绑为公开 Plugin，可按官方插件流程提供 Skill；MCP Skills Extension 的导入
发生在提交门户的「Scan Tools」阶段，并非每次运行时动态读取。私有连接页的刷新工具
不等于已经完成公开提交或自动导入 Skill。
见 [插件 Skill 构建规范](https://developers.openai.com/plugins/build/skills)。

## 本轮证据边界

已核实网页入口和编辑器字段，通用 Skill 文件已编写；网页创建/启用和自然问题的实际
自动匹配结果应以本轮最终验收记录为准。仅有本机文件不能称作网页已安装成功。
