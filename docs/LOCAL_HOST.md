# Colink · 本机托管与 ChatGPT 私有连接

直接安装用户优先阅读 [INSTALL.md](INSTALL.md)。下面是**从源码运行**的通用配置，
不是维护者个人账户的可复用连接；每位使用者必须取得自己的 Tunnel ID 和授权。

## 访问范围

最初的网页接入授权与源码验收仅限自带 `examples/sample_project`，下面列出的是该
样例基线配置。菜单栏后续已保存的选择以本机界面为准；更换来源必须在本机确认，
不能从网页扩大读取范围。0.3.3 维护保留更新前的同一文件夹选择，未添加目录；网页
只验证项目/文件数量，不把它称为新的真实项目源码验收。采集与 MCP 查询仍是两个
职责，源码目录只读，不增加命令执行或写文件工具。

官方 Secure MCP Tunnel 由本机主动发起 HTTPS；无需开放本机公网入站端口。它不是
完全离线方案：获授权的网页调用返回的样例源码会传给 OpenAI。只读工具与基础凭据
过滤不是系统沙箱或完整 DLP。不要把其他私有源码复制进样例目录。
参考：[官方 Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。

## 样例基线配置与现有连接

- 根目录：你克隆的 `Colink/examples/sample_project`（配置生成时解析为本机绝对路径）。
- 项目 ID：`sample`
- 独立持久化目录：`.code-context/local-sample/`，不复用旧的 snapshot/HTTP 镜像目录。
- 专用运行密钥：项目 `.env.local` 的 `OPENAI_API_KEY`，权限 `600`，匹配现有 Git 忽略规则。
- 有效 profile：`.code-context/tunnel/profile.yaml`，权限 `600`，无密钥。
- Tunnel：使用者自行创建并确认的 Tunnel，关联自己的组织与目标 ChatGPT 工作区。
- ChatGPT 网页插件显示名：`Colink`，与 macOS 应用同名；只是重命名原连接，
  首次接入需要按下方官方流程创建使用者自己的连接。

API Key 是隧道运行凭据，不是网页登录凭据；本 MCP 不会自行发起模型调用。
创建 Tunnel、运行客户端、选择网页连接分别受组织/工作区权限控制；密钥存在不等于
权限或网页调用已通过。未安装额外 Codex Tunnel 插件，不影响这条 ChatGPT 连接路径。
本次实际连接和读取证据见 [VALIDATION.md](VALIDATION.md)，不是根据密钥存在推断成功。

## 官方客户端

由官方文档链接的 [最新发行版入口](https://github.com/openai/tunnel-client/releases/latest)
下载适合 Apple Silicon macOS 的客户端。本次核验版本为 `0.0.15`，下载包保留在：

`.artifacts/tools/tunnel-client-v0.0.15/tunnel-client-v0.0.15-darwin-arm64.zip`

SHA-256 与 GitHub 官方发行资产元数据一致：

`b2cae3aa9df45b4c2fe9b1d700ebacce39f9feb6a6b46b86e6499f9a51bf72ff`

实际执行路径：`.artifacts/tools/tunnel-client-v0.0.15/extracted/tunnel-client`。
解压包也含官方 cloudflared 等组件；本项目 profile 没有配置公网 URL 转发或其他本机服务。
升级时重新从 latest 入口选择对应平台，并核对校验值，不覆盖原始下载/旧产物。

## 日常检查与启动

macOS 用户优先使用“应用程序”中的 **Colink.app**（安装到 `/Applications`），通过菜单栏“启动”“关闭”
管理连接，详见 [MACOS_APP.md](MACOS_APP.md)。下面命令保留供开发和故障检查，
不要与菜单栏应用同时启动。退出应用或关闭连接后均不会自动重新运行。

在项目目录执行，以下命令不会把密钥放入参数或输出：

```sh
cd /absolute/path/to/Colink
uv sync --locked
uv run colink tunnel-doctor \
  --client .artifacts/tools/tunnel-client-v0.0.15/extracted/tunnel-client
uv run colink tunnel-run \
  --client .artifacts/tools/tunnel-client-v0.0.15/extracted/tunnel-client
```

不要同时手工启动 `local` 并让 Tunnel 启动同一个数据目录；单进程锁会拒绝第二个来源。
`doctor` 检查配置与本地可用条件，不替代真正的网页工具调用或控制面权限验收。
`tunnel-run` 需要保持运行；Ctrl+C 停止，不删除镜像、profile 或密钥。

另一个终端只读检查：

```sh
uv run colink local-status --data-dir .code-context/local-sample
uv run colink tunnel-status
```

`tunnel-status` 的健康/就绪只表示本机健康端点的响应，`chatgpt_web_verified` 始终为
`false`，不能冒充端到端验收。此项证据应在 [VALIDATION.md](VALIDATION.md) 单独记录。
管理 UI 地址来自该命令的 `admin_ui_url`，只监听 `127.0.0.1`。

## 运行安全检查

包装程序不 `source`/执行 `.env.local`，只读取唯一有效的密钥赋值；拒绝符号链接、
硬链接、非私有权限和超大配置。实际密钥仅经进程环境传给官方客户端，本机 MCP
启动后移除它，不进入 profile、命令行、源码或 outbox。

配置限定官方控制面、单一受限 stdio 命令和回环管理端点，拒绝额外命令/HTTP 转发；
清除无关客户端设置环境变量，禁用原始 HTTP/源码载荷日志，展示输出中的运行密钥
也会脱敏。直接绕过包装程序运行官方客户端不具有这些项目级配置检查。

本机每个项目只保留当前和前一次代码状态；更早快照与无引用内容自动清理。
修改 `.codecontextignore` 只影响后续采集，不立即清除仍保留的前一次内容，也不能撤销
已传给 OpenAI 的源码；需要主动删除/撤销共享时，应明确指定范围后另行处理。

## 新建或更换连接

变更真实目录或访问范围前先确认。先 `scan` 检查允许文件，再选新的独立数据目录。
获取实际 Tunnel ID 后可生成新 profile，禁止静默覆盖已存在配置：

```sh
uv run colink tunnel-init --root examples/sample_project --project sample \
  --data-dir .code-context/local-sample --tunnel-id tunnel_REPLACE_WITH_YOUR_REAL_ID \
  --output .code-context/tunnel/new-profile.yaml
```

JSON 语法兼容 YAML，但官方客户端要求 profile 扩展名为 `.yaml`。早期 `.json`
检查产物保留，不是有效的运行配置。新 profile 需通过 doctor 和实际网页重新验收。

按 [官方网页连接指南](https://developers.openai.com/plugins/deploy/connect-chatgpt)，
在 ChatGPT 插件页添加自定义 MCP，连接选择 Tunnel，填入你自己的正确 ID。当前本机 stdio
不提供额外 OAuth 服务；选择无需额外 MCP 身份验证时，访问边界仍是私有 Tunnel 的
组织/工作区授权，而不是匿名公网服务。只发现五个只读工具后，再进行真实对话读取。
具体 UI 以账户实际页面为准，不擅自更改其他安全选项。

## 未自动进行的操作

未开放防火墙/路由器端口，未配置公网 HTTPS，未修改睡眠设置或安装登录自动启动服务。
电脑断网、休眠、关机或客户端退出时，网页会暂时不可用；重启后先对账恢复代码修改。
测试、构建、下载、临时配置和持久化数据库全部保留，不自动清理。
