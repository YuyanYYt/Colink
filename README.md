<p align="center">
  <img src="macos/assets/logo.svg" width="96" alt="CoLink logo">
</p>

<h1 align="center">CoLink · 连接你的代码</h1>

<p align="center">
  让网页 AI 看懂本机项目，并在你允许时直接修改代码。<br>
  一款轻量的 macOS 菜单栏应用。
</p>

<p align="center">
  <a href="https://github.com/YuyanYYt/Colink/releases/tag/v0.5.0b2">下载 macOS 安装包</a> ·
  <a href="docs/INSTALL.md">安装与使用</a> ·
  <a href="docs/AGENT_INSTALL.md">让 Agent 帮你安装</a> ·
  <a href="https://github.com/YuyanYYt/Colink/releases">历史版本</a>
</p>

**当前版本：0.5.0 Beta 2（`0.5.0b2`）** · macOS 14+ · Apple Silicon · [MIT](LICENSE)

CoLink 把你选定的本机项目接入 MCP。你可以在 ChatGPT 网页端询问项目结构、查找
函数、理解跨文件依赖；需要改代码时，在本机打开写入开关，让 AI 精准编辑、创建
或删除文件。默认只读，项目访问范围始终由你在本机确认。

## 能做什么

| 功能 | 你可以用它做什么 |
| --- | --- |
| 按需读取源码 | 查找文件、搜索代码、读取指定行或片段；直接读取磁盘上已保存的文件|
| 多项目管理 | 选择一个工作目录，分别启用其中的项目；在对话中指定项目|
| Python / Java 代码关系 | 查找符号、定义和引用，按需查询类关系、调用关系、文件/模块依赖、反向影响和依赖层级 |
| 精准写入 | 本机授权后按行插入、替换指定范围或唯一片段；修改前核对原文和文件哈希 |
| 创建与删除 | 创建文件和目录，删除明确指定的单个文件；不提供递归删除 |
| 任务 Diff | 查看同一任务跨多轮、跨文件的累计改动 |
| 菜单栏控制 | 启动、关闭、选择文件夹和管理项目；跟随系统深浅主题，|
| 可选自动匹配 Skill | 本机代码缺少上下文时优先读取；已经贴出足够代码时直接回答，无需每次手选技能 |

普通文本源码读取不限于 Python / Java；结构化关系分析目前重点支持这两种语言。
关系来自静态分析，动态调用、外部库内部实现等可能无法确定，工具会标明不确定项。
Skill 是否自动匹配由客户端决定，不保证每轮触发。

## 开始使用

### 1. 安装应用

打开 [0.5.0 Beta 2 下载页](https://github.com/YuyanYYt/Colink/releases/tag/v0.5.0b2)，
下载 `Colink-macos-arm64.dmg`，将 `Colink.app` 拖入“应用程序”，然后打开 **CoLink**。
也可以下载 ZIP 安装。

安装包已包含 Python、MCP 运行依赖和隧道客户端，**不需要先安装开发环境**。
目前支持 Apple Silicon Mac（M 系列芯片），macOS 14 或更新版本。
Beta 包尚未完成 Apple Developer ID 签名和公证，首次打开可能出现系统安全提示；
详情与文件校验方法见 [安装指南](docs/INSTALL.md)。

### 2. 配置自己的连接

在菜单栏面板完成“首次连接设置”，填写你自己的 Tunnel ID 和运行密钥，再在
ChatGPT 中添加对应的私有 MCP 连接。首次配置步骤见
[连接指南](docs/INSTALL.md#只需首次完成你自己的-chatgpt-私有连接)。

连接通过出站私有隧道建立，不需要 VPS、路由器端口映射或本机公网入站端口。
你仍需要具有相应功能和权限的 ChatGPT / Platform 账号；安装软件不会自动获得这些权限。

### 3. 选择项目并启动

先用自带样例确认连接可用，再选择自己的项目或工作目录。在“项目管理”中启用
要提供给 AI 的项目，点击“启动”。新发现的项目不会自动获得访问许可。

在新对话中可以这样问：

> 看看我已连接的项目有哪些。请只读取项目清单。
>
> 解释指定项目的入口函数，并查一下它依赖哪些文件。
>
> 找到这个 Python 类的定义、调用位置，以及修改它可能影响的模块。

需要修改时，再打开本机的 **“允许修改代码”**，选择准确项目，并明确告诉 AI
要改什么。关闭连接、退出或重启后，写入权限都会关闭。更新旧版后，应在 ChatGPT
原连接页刷新工具并新建对话。写入使用方法见 [读取与写入指南](docs/LIVE_USAGE.md)。

### 让 Agent 协助安装

把下面这句话交给你已有的本机编码助手：

> 请按照 https://github.com/YuyanYYt/Colink/blob/main/docs/AGENT_INSTALL.md
> 帮我安装 CoLink，先检查系统、版本和校验值，再说明安装计划；保留现有配置，
> 由我自己完成登录、密钥输入和项目授权。

Agent 可以协助下载、校验和安装，但不能代替账号授权或绕过系统安全提示。
想减少日常手动选择，另见 [自动匹配 Skill 的安装方法](docs/CHATGPT_SKILL.md)。

## 从源码运行

适合开发者、Linux 用户或使用本地 stdio MCP 客户端的用户。准备 Git、Python 3.11+
和 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后运行：

```sh
git clone https://github.com/YuyanYYt/Colink.git
cd Colink
uv sync --locked
```

让支持 stdio 的 MCP 客户端在仓库目录启动以下命令，即可只读连接自带样例：

```sh
uv run colink live --root examples/sample_project --project sample \
  --data-dir .code-context/live-sample
```

这不是网页访问地址，不会自动配置 ChatGPT 或打开写入权限。
多项目、HTTP、旧镜像兼容入口和客户端配置见 [命令行指南](docs/CLI_USAGE.md)。

## 版本更新

这里只列使用者能感受到的变化。开发阶段也保留在表中，不代表每个阶段都有独立安装包；
已发布包和对应源码可在 [Releases](https://github.com/YuyanYYt/Colink/releases) 找到。

| 版本 | 主要变化 |
| --- | --- |
| **0.5.0 Beta 2 · 当前** | 改善无 Git 项目发现和同名项目区分；发布直读、多项目及受控写入安装包；移除主动代码回退，代码历史交给项目 Git 管理 |
| 0.5.0 Beta 1 | 原文件按需直读、多项目授权、默认关闭的精准编辑与文件/目录创建、任务 Diff；后续源码补充单文件删除 |
| 0.4.3 · 开发阶段 | 修复自定义 Python 源码根的模块定位，支持显式源码根配置 |
| 0.4.2 · 开发阶段 | Python / Java 符号与关系查询；单文件上限提高到 4 MiB；品牌统一为 CoLink |
| [0.4.1](https://github.com/YuyanYYt/Colink/releases/tag/v0.4.1) | Diff 摘要、分页读取、连接状态检查、文件过滤与自动匹配 Skill |
| [0.4.0](https://github.com/YuyanYYt/Colink/releases/tag/v0.4.0) | 首个公开自包含 DMG / ZIP、首次连接设置、菜单栏运行和 Agent 安装指南 |
| 0.3.3 | 项目与插件统一品牌；旧镜像只保留当前和前一次代码状态，取消面向用户的递增版本编号 |
| 0.3.2 | 插件名称统一，Logo 间距与中心对称优化 |
| 0.3.1 | 精简界面、平滑 Logo，支持从“应用程序”重新打开 |
| 0.3.0 | 原生 macOS 毛玻璃菜单栏界面，加入启动、关闭和选择文件夹 |
| 0.2.0 | 本机持续托管和私有隧道连接，无需另购服务器 |
| 0.1.0 | 初版源码读取、搜索、Diff、同步和 stdio / HTTP MCP |

完整记录见 [CHANGELOG](CHANGELOG.md)。更新应用请先关闭并退出旧版，保留原配置与数据；
历史版本可下载，但不保证能直接读取新版数据格式，详见 [升级说明](docs/INSTALL.md#升级已有安装)。

## 使用前需要知道

- **你决定可读范围。** 只开放本机已确认的项目；不要直接共享个人主目录、系统目录或凭据目录。
- **私有连接不等于代码不出电脑。** 被查询到的源码会发送给使用它的 AI 客户端及其服务提供方。
- **写入默认关闭。** 本机许可和客户端工具确认相互独立；不会自动授予“全部权限”。
- **代码历史使用 Git。** 当前版没有主动代码回退按钮；修改前保存重要稳定点，任务 Diff 不是 Git 历史。
- **不提供终端执行。** 当前不能替网页 AI 安装项目依赖、运行项目或提交仓库，这些仍是后续规划。
- **存储有界，但不是零。** 读取不保留完整源码镜像；结构索引、任务比较起点和中断保护仍占空间，并有容量限制。

过滤规则、隐私与写入边界见 [SECURITY](SECURITY.md)。

## 更多资料与贡献

[安装与升级](docs/INSTALL.md) · [读取与写入](docs/LIVE_USAGE.md) ·
[Python / Java 代码关系](docs/CODE_INTELLIGENCE.md) · [自动匹配 Skill](docs/CHATGPT_SKILL.md) ·
[技术架构](docs/ARCHITECTURE.md) · [开发与贡献](CONTRIBUTING.md)

欢迎通过 [Issues](https://github.com/YuyanYYt/Colink/issues) 提交问题和功能建议。
请附系统、芯片、应用版本和复现步骤，不要上传运行密钥或私人源码。

CoLink 使用 [MIT 许可证](LICENSE)。这是每位用户独立运行的开源软件，不是共享维护者
账号的公共连接，也不代表已经上架 ChatGPT 公共插件目录。
