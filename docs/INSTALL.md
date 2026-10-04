# 安装与使用 Colink

## 推荐：直接安装 macOS 应用

首版安装包支持 **Apple Silicon（M1/M2/M3/M4 等）和 macOS 14 以上**。
Intel Mac、Windows 没有对应的图形安装包。macOS/Linux 开发者仍可使用 Python CLI。

1. 打开 [GitHub v0.4.0 下载页](https://github.com/YuyanYYt/Colink/releases/tag/v0.4.0)。
2. 下载 `Colink-macos-arm64.dmg`，双击打开，将 `Colink.app` 拖到“应用程序”。
   也可下载 ZIP，解压后把应用移到 `/Applications` 或 `~/Applications`。
3. 从 Finder 的“应用程序”打开 Colink，点击菜单栏图标。
4. 点击“首次连接设置”，填写你自己的 Tunnel ID 和隧道运行 API Key。
5. 保存后仍为关闭状态；先用样例验证，再选择具体代码文件夹、确认共享并点击启动。

应用包含运行环境，不依赖开发者的电脑目录，也无需先安装 Python/uv/Node。
打开应用不启动代码采集；没有开机自启、自动重连或关闭后自动恢复。
运行时只显示菜单栏图标，不占 Dock 位置；仍能从“应用程序”或应用搜索打开。

这是开源预览版，仅采用本地 ad-hoc 签名，**未完成 Apple Developer ID 签名或公证**。
macOS 可能提示无法验证开发者。核对项目和校验值后，是否使用系统提供的单个应用
“仍要打开”入口由你决定；安装脚本和 Agent 不会清除 quarantine、关闭 Gatekeeper 或
绕过系统警告。想要无这些提示的正式分发仍需后续签名与公证。

下载页同时提供 `SHA256SUMS`。例如在下载目录执行：

```sh
shasum -a 256 -c SHA256SUMS
```

把需要校验的安装文件和校验清单放在同一目录。校验值验证文件完整性，不等于 Apple
公证，也不是独立的发布者身份认证。

## 只需首次完成：你自己的 ChatGPT 私有连接

Colink 本身不调用模型，不需要模型调用密钥。以下运行 API Key 只交给官方私有隧道
客户端；拥有密钥不代表已经获得 Tunnel 或 ChatGPT 工作区权限。

1. 在 [OpenAI Platform 私有隧道设置](https://platform.openai.com/settings/organization/tunnels)
   创建自己的 Tunnel，关联自己的 Platform 组织及需要使用的 ChatGPT 工作区。
2. 准备可使用该 Tunnel 的运行 API Key。把 Tunnel ID 和密钥填入 Colink 的首次设置；
   不要复制到 GitHub、聊天消息、截图或项目源码里。
3. 在 Colink 点击启动。保持电脑联网、应用运行，不需要开放路由器或防火墙公网入站。
4. 根据自己的账户权限，在 ChatGPT“设置 → 安全和登录”启用开发者模式。
5. 在 ChatGPT 插件/连接管理页添加自定义 MCP，名字填写 `Colink`，连接方式选择 Tunnel，
   选择自己的 Tunnel 或填入同一个 ID。权限和工作区必须与你的 Tunnel 一致。
6. 确认发现下面五个只读工具，再在新对话中启用 Colink。

账户/工作区是否提供开发者模式和私有隧道，以当前账户页面和管理员策略为准。
不能在你的账户创建 Tunnel 时，本机 CLI/stdio 仍可用，但本项目不会暗中切换为公网代理。
官方边界见 [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
和 [连接与测试](https://developers.openai.com/plugins/deploy/connect-chatgpt)。

此处发布的是每个人自己运行的开源软件，不是所有人共享同一个个人插件。
私有 Tunnel 可以用于开发者模式连接，但不代替 ChatGPT 公共插件目录的公网端点、
认证及发布审核要求。

## 第一次验证

先只连接自带样例，在新对话中询问：

> 使用 Colink，仅调用 list_projects，告诉我当前连接的项目和文件数量。

再询问：

> 使用 Colink 查看样例 main.py，解释入口函数。先获取概览，再固定同一读取标识读取。

五个工具为 `list_projects`、`repo_overview`、`read_file`、`search_code`、`get_diff`。
确认网页确实读到样例后，再关闭连接，选择自己的具体项目，确认源码查询会发送给
OpenAI，点击启动。不要直接共享 Home、系统目录、凭据或其他人的私有代码。

工具始终只读，不提供执行命令或修改文件的能力。正常回答不需要展示递增版本编号；
每个项目只保留当前和前一次代码状态，过期读取标识会明确拒绝，不会悄悄混入新代码。

## 日常操作与数据

| 操作 | 结果 |
| --- | --- |
| 选择文件夹 | 仅选择；尚未开始采集或连接 |
| 启动 | 采集所选项目，启动本机镜像和出站私有隧道 |
| 关闭 | 停止本应用创建的连接和采集，直到再次手动启动 |
| 收起面板 | 只收起界面，连接继续运行 |
| 退出应用 | 先停止本应用的连接，再退出；重开仍关闭 |

运行中不能更换目录，先关闭。休眠、断网或关机时网页不可用；重新手动启动会对账
停止期间的代码改动。用户数据在 `~/Library/Application Support/Colink/`：私有密钥
文件 `.env.local`、profile、每个来源独立镜像与样例。密钥文件权限为 `600`，未使用
系统钥匙串。关闭或卸载应用**不会自动删除这些持久化数据**。

两份源码不是整个数据目录的固定大小上限：还包括索引、同步状态、SQLite 空闲页及
短期 WAL/SHM；不同已选择来源各自保存两份状态。基础过滤不是完整敏感信息检测。

## 从源码安装（开发者、Linux 或本地 MCP 客户端）

准备 Git、Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```sh
git clone https://github.com/YuyanYYt/Colink.git
cd Colink
uv sync --locked
uv run colink demo-local
```

这一步不读取 API Key，不连接 OpenAI，不安装开机服务。成功结果是 `status: passed`。
支持 stdio 的本地 MCP 客户端可以使用：

```sh
uv run colink local --root examples/sample_project --project sample \
  --data-dir .code-context/local-sample
```

网页私有连接的源码配置见 [LOCAL_HOST.md](LOCAL_HOST.md)；从源码构建图形应用见
[MACOS_APP.md](MACOS_APP.md)。HTTP/HTTPS 模式、容量限制与协议见 README 和
[ARCHITECTURE.md](ARCHITECTURE.md)。

## 常见问题

- “已有连接”：不要同时让另一个终端或应用运行同一个 Tunnel，先在原窗口停止。
- “先设置连接”：填写你自己的设置；不允许覆盖已有配置。部分写入失败时不要盲目
  删除文件或反复安装，按报错检查用户目录权限，必要时开不含密钥的 GitHub Issue。
- 移动应用后配置不可用：profile 绑定运行解释器路径。请先把应用放在最终安装位置
  再做首次设置；已配置后改位置需手动迁移 profile，不能照搬别人生成的配置。
- 已设置后需要换密钥或 Tunnel：首版设置只做首次配置，不提供静默覆盖。先关闭连接，
  在用户目录手动检查/更新自己的私有文件；不要把旧配置传给 Agent 或提交到 GitHub。
- 网页工具仍是旧参数：重启后端，在原连接页刷新工具并新建对话；不要复用过期的
  数字版本参数。

遇到问题请报告系统/芯片、版本、操作步骤和不含凭据的错误信息。
