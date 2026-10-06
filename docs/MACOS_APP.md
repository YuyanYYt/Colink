# Colink · macOS 菜单栏应用

2026-10-06：下方为已公开镜像版的安装/运行说明。未发布 0.5.0-beta.1 新模式默认
原文件直读，增加「项目管理」「允许修改代码」和整项回退；关闭/重启不继承写入授权。
新用法见 [LIVE_USAGE.md](LIVE_USAGE.md)，实际与待验项见
[WEB_WRITE_VALIDATION.md](WEB_WRITE_VALIDATION.md)。仅显式 `--source-mode live`
构建的新包使用直读；默认旧构建参数仍兼容镜像。不要给旧版套新语义。

维护者在 2026-10-06 单独授权更新后，已将通用 0.5.0b1 安装到 `/Applications/Colink.app`。
实际样例网页小改、菜单栏回退终态、正常关闭/退出重开默认关闭及网页拒绝均已补验。
原 0.4.3 应用移至废纸篓可恢复，旧配置/凭据/数据库和固定锚点保留；没有公开发行。

## 两种构建，不要混用

- **发行安装包**：GitHub Releases 的 DMG/ZIP 是自包含 Apple Silicon 应用，内含
  Python、锁定的运行依赖和官方私有隧道客户端。用户数据在自己的
  `~/Library/Application Support/Colink/`，通过首次连接设置填写自己的信息。
- **源码开发构建**：`macos/build.py` 的小应用引用克隆目录及该目录的依赖，只适合
  构建它的电脑；不能把这种约 1.2 MiB 的开发 bundle 当作独立安装包发布。

首次用户请按 [INSTALL.md](INSTALL.md)，自动化助手按 [AGENT_INSTALL.md](AGENT_INSTALL.md)。

## 直接使用

推荐安装到 `/Applications/Colink.app`，也可使用自己的 `~/Applications`。
发行版不需要保留克隆目录；维护者工作区中的旧开发副本不是独立发行安装包。
在 Finder 的“应用程序”中双击 Colink，再点击顶部图标即可操作。
退出或电脑重启后，不用先开终端或找到项目目录。

macOS 的应用列表/应用搜索中也可按名字找它；快捷搜索入口为 ⌘ Space。
0.3.3 曾实际验证 Finder 图标重新打开，系统 Spotlight 索引也识别了安装路径；当时系统
Apps/Spotlight 窗口无法读取，因此不把索引检查写成四指手势
或 Spotlight 界面实测。应用入库方式参考
[Apple：查看和打开 App](https://support.apple.com/guide/mac-help/open-apps-in-spotlight-mh35840/mac)。

0.4.0 在用户单独确认后将 `LSUIElement` 设为 true，从启动起声明 agent 应用，
同时保持 accessory 策略；应用仍安装在“应用程序”中，不需要固定在 Dock。
没有修改系统 Dock 设置、手势或登录启动。原已安装开发版不会被打包流程覆盖。

| 操作 | 实际行为 |
| --- | --- |
| 启动 | 启动本机镜像和官方私有隧道；状态来自实际镜像锁和健康端点 |
| 关闭 | 停止应用创建的隧道/采集进程组；没有自动重启、自动重连或运行意图恢复 |
| 选择文件夹 | macOS 原生文件夹选择；仅保存选择，不扫描、上传或启动 |
| 收起右上角面板 | 只收起界面，正在运行的连接继续工作，不等于“关闭” |
| 退出应用 | 先停止自己托管的连接，再退出；重开应用默认仍关闭 |

运行中选择按钮禁用，先关闭后更换目录。主面板只保留状态、文件夹、启动/关闭、
打开 ChatGPT 和退出。快照、文件数量、隧道等技术指标不在日常面板展示；只有发生
错误时才显示相关操作提示。共享说明保留在其他目录的启动确认中，不重复占用面板。
本机状态不是一次真实网页调用的证明，网页验收另外记录。

## 目录与共享边界

默认自带 `examples/sample_project`，0.3.0–0.3.2 的完整网页源码验收只使用这个目录。
0.3.3 安装保留更新前已有的目录选择，并恢复同一连接；没有选择新来源，本轮网页
只查询项目/文件数量。你主动选择其他真实代码目录并点击启动时，会弹出确认，
说明源码在网页查询时传给 OpenAI、仅只读、
只保留当前与前一次代码状态，基础过滤不是完整敏感信息检测；取消不会启动。不要把无关私有文件
放入待共享项目。应用拒绝直接选择整个 Home、系统根目录等过宽范围。

每位使用者自行配置一个私有 Tunnel/组织/工作区；应用不创建新远程权限。ChatGPT 网页插件与
菜单栏产品统一叫 `Colink`。不要复制维护者的连接 ID、运行密钥或 profile。更换目录会替换该连接当前
提供的项目，并使用独立本机镜像；不会将原项目旧快照冒充新项目，也不删源文件。
每个独立镜像各保留两份代码状态，较早快照会按已确认策略自动清理。

原样例沿用 `.code-context/local-sample/`；其他路径以完整 SHA-256 分离在
`.code-context/desktop/sources/<path-hash>/`。每个来源保持独立 project/data/profile。
关闭连接不撤销已经共享的代码或删除数据库。需要撤销/删除时必须明确指定范围。

## 关闭、崩溃和外部连接

原生应用保持到本机监督进程的控制管道；发送关闭请求或应用退出/崩溃导致管道 EOF，
监督进程都会停止自己创建的完整进程组。没有 LaunchAgent、登录自动启动服务或
自动重启循环。电脑重启后需要手动打开应用、点击启动。

如果状态显示“已有外部连接”，先在原来的终端/应用中停止它。Colink 不会
杀掉其他应用的进程；也不要同时为同一个 Tunnel 启动两个 stdio 客户端。
整个连接未停止时不会宣称关闭成功；状态异常时也不会将本机就绪冒充网页验收。

## 凭据和依赖

源码模式使用项目 `.env.local`，发行应用使用用户数据目录的 `.env.local`，权限 `600`。
首次设置使用 SecureField，密钥只通过 stdin 交给本机配置程序，不进入命令参数；
UI 不读取已经保存的密钥，应用包不包含用户密钥。安全 launcher 加载它交给官方
客户端，没有模型调用。首版不自动覆盖配置，也尚未使用 Keychain。
仅管理页面监听 `127.0.0.1`，不开放公网入站；仍是需要联网的私有连接。
参考：[官方 Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)。

源码开发构建针对 Apple Silicon，目标 macOS 14+，本地验证环境为 macOS 27.0.1。
开发构建引用本项目路径、uv、`.venv` 和已核验官方客户端，不是独立分发安装包；
可以移动应用本身，但不要移动/删除后端项目及依赖。运行环境丢失会在 UI 报错，不会
静默安装依赖、改写凭据或扩大目录范围。

## 设计与维护

- UI：原生 SwiftUI + AppKit `NSVisualEffectView`，主题适配的毛玻璃、系统字体、
  圆角和系统控件。当前系统深色主题有实际截图检查；没有擅自切换全局系统主题。
- Logo：`macos/assets/logo.svg`，代码括号与连接链组合；以画布中心 `(128, 128)`
  严格中心对称，右括号由左括号旋转 180° 生成，链条及渐变也使用同一中心。
  两侧等量留出空隙，连接链保持单条连续相切曲线；代码括号也软化转角。菜单栏源图是
  `macos/assets/menubar.svg`。PNG/ICNS 从这两个 SVG 构建派生，原始 SVG 一直保留。
- 原生代码：`macos/CodeConnect/`；本机生命周期：`src/code_context/desktop.py`。
- 应用偏好仅记住选择目录，不保存“下次自动运行”。没有额外目录/网络权限工具。

后续构建选新的输出路径，不覆盖或删除早期构建：

```sh
uv sync --locked
uv run python macos/build.py --output ".artifacts/apps/next-build/Colink.app"
```

构建使用 Swift 工具链和 Node/sharp 渲染 SVG，仅进行本地 ad-hoc 签名；
所有输出目录统一复用项目根目录 `swift-module-cache/`，不再为每个版本重复创建一份
SDK 模块缓存。该目录只在开发构建时使用，已安装应用运行不需要它；工具链或 SDK
更新仍可能在共享缓存中生成新的模块。旧目录只在确认具体清理范围后处理。

应用未进行 Apple Developer ID 签名、公证或 App Store 发布。构建命令默认不安装；
0.4.1 已在维护者本机安全更新到 `/Applications` 并保留旧应用；打包命令本身不安装。
应用启动只注册自己的 bundle，不重建或清理其他应用的 Launch Services/Spotlight。
不同电脑先 `npm ci --prefix macos`；默认从 PATH 查找 Node，使用 `macos/node_modules/sharp`。
用 `--client` 指定已核验的官方客户端，可用 `--node`/`--sharp` 显式指定构建依赖。
默认 ChatGPT 按钮打开通用插件页，不绑定任何维护者账户。

发行包构建使用 `macos/package.py`，从同一 `uv.lock` 安装后的环境提取运行依赖，
复制可重定位的 CPython、源码、样例和官方客户端许可文件；runtime.json 仅含 bundle
内部相对路径。新输出目录必须不存在，不覆盖任何已安装应用。

```sh
uv sync --locked
npm ci --prefix macos
uv run python macos/package.py --output-dir .artifacts/releases/colink-0.4.1-new-build
```

打包会生成 DMG、ZIP、SHA256SUMS 和报告。校验搬移后的解释器、CLI/MCP 导入与
应用完整性后，再审查归档无凭据/个人路径，才可上传。原生文件包含第三方运行组件，
不是单纯约 1.2 MiB 的界面壳；发布大小以最终产物为准。

应用、图标源、派生图标、早期应用版本和测试目录仍保留。用户确认后，仅删除了
`dist/` 及 4 个历史构建输出目录中的重复 Swift 编译缓存，保留根目录共享缓存；
后续清理仍须确认具体范围。不能把 `.code-context` 或真实代码目录当成可清理缓存。
