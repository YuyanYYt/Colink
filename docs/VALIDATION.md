# 验证记录

公开副本已经去除私人会话链接、个人插件/Tunnel 标识和个人路径。原始验收记录
只保留在维护者本机未提交的归档中；下述历史结果不是每个新账户的可用性保证。

## 0.4.2 · 独立读取增强分支的本地验证

日期：2026-10-05（Asia/Shanghai）。分支 `codex/colink-code-intelligence`，版本
0.4.2。范围为 Python / Java 静态查询、大项目读取配额和 CoLink 品牌文字；不启用
写入、不扩大来源、不移动锚点、不替换安装版、不更新个人账号的插件或 Skill。

| 检查 | 实际结果 |
| --- | --- |
| 全量回归 | 430 passed，29.54 秒；1 条原有 Starlette/AnyIO 弃用提示 |
| 定向复核 | 最后补充的关系、分页及实际 stdio 子进程用例 34 passed，9.53 秒 |
| Ruff | src/tests/scripts/macos 的 check 与 format check 均通过，45 个 Python 文件 |
| 公开源码检查 | Publication guard 87 files / 0 findings；55 个本地 Markdown 链接均存在，不等于完整 DLP/安全认证 |
| 锁定依赖 | `uv sync --locked`；新增 tree-sitter 0.26.0 与 tree-sitter-java 0.23.5，保留 uv.lock |
| MCP 子进程 | Python 和 Java 分别实际调用全部九个新增结构工具，复用 snapshot 并读取原始源码 |
| 四类关系 | 继承/实现/成员、调用/实例化、模块文件正反向依赖、外部依赖候选与证据行通过 |
| 保守性 | 局部/参数遮蔽、self 重绑定、条件重导出、Java 同参数数重载不认作确定目标；嵌套类/静态导入通过 |
| 图与层级 | 多文件与模块自身导入循环、依赖优先层级、反向影响链、有界响应通过 |
| 查询隔离 | 禁用解析/索引构建后仍能查询，前后数据库逻辑导出一致；并发清理旧状态时读事务仍固定旧事实 |
| 更新与恢复 | 单文件变更仅重新解析该文件；定义改名重绑未变导入者；删除/重命名/语法错、重启回填、幂等/冲突、失败整体回滚通过 |
| 大项目配额 | 单文件 4 MiB、总文本 128 MiB、50000 文件、256 MiB JSON；旧上限以上的 10005 文件、总量及超过 20 MiB 请求回归通过 |
| 源码续页 | 长单行、中文/emoji、引号/反斜杠逐字拼接一致；符号读取不越过定义，不跳过或重复内容 |
| 有界索引 | 每状态解析/物化负载各最多 32 MiB、最多 100000 符号＋关系；超限显式 partial，源码仍可读 |
| HTTP 演示 | passed，发现十五工具，完整/增量、幂等重试、崩溃恢复、源码隔离和敏感文件排除 |
| 持续 stdio 演示 | passed，热更新、停机编辑重启恢复、两份保留、过期拒绝及无数字 MCP 版本参数/结果 |
| Skill | 本地 SKILL.md 官方校验通过；按问题选工具，保留 allow_implicit_invocation=true，不代表账号内旧 Skill 已更新 |
| 原生开发构建 | Swift 编译与 deep/strict ad-hoc 签名通过；CoLink / 0.4.2、LSUIElement=true，原 bundle ID 保持 |
| 安装与网页 | 未覆盖安装版、未做新工具网页验收；开发 app 引用项目 uv/依赖，不是自包含 DMG/ZIP，不宣称已发布 |

### 小改保留与合成性能测量

约 100 KiB Python 文件连续小改 300 次后，源码状态、索引状态和解析缓存各 2 条；
源码正文 204782 bytes，SQLite 本体 434176 bytes（424 KiB）。除正文外还包含结构
索引、清单、元数据、页和空闲页等，不是 300 份副本，也不构成任意项目的固定体积。
数据库行与 WAL 的实际占用随项目大小/读事务变化，测试的上限不等于物理目录硬上限。

用本机数据库构造 102 / 1002 个很小的 Python＋Java 源文件，每类查询 30 次，
每场景小改 10 次；不调用网页、模型或实际业务。以下为本轮合成测量，不推广为
任意仓库的性能保证，源码样本远小于实际大型项目。

| 测量 | 102 文件 | 1002 文件 |
| --- | --- | --- |
| 首次源码＋索引提交 | 41.917 ms | 328.114 ms |
| 精确符号查询，中位数 | 0.393 ms | 0.356 ms |
| 原文本搜索，中位数 | 0.369 ms | 0.966 ms |
| 反向文件图查询，中位数 | 0.948 ms | 5.810 ms |
| 单文件小改提交，中位数 | 21.201 ms | 235.651 ms |
| 最后更新解析 / 复用文件 | 1 / 101 | 1 / 1001 |
| 最后更新重解 / 复用关系文件 | 1 / 101 | 1 / 1001 |
| 保留索引状态数 | 2 | 2 |
| 最终 SQLite 本体 | 2985984 bytes | 28344320 bytes |

小仓库符号查询没有比文本搜索更快；1002 文件场景的精确查询更快，但首次解析、
索引空间和提交仍增加成本。SQLite 索引/行等开销可明显大于这些很短的合成源码；
不将解析复用说成整次更新严格 O(变更文件)。测量进程峰值 RSS 为 70434816 bytes，
不是已安装菜单栏应用的内存测试，也没有端到端模型响应/生产吞吐结论。

### 留存与早期失败

- 最终全量：`.artifacts/test-runs/colink-verified.N3riCn/`。
- 定向补充：`.artifacts/test-runs/colink-extra.SR7B0K/`。
- 先前回归：`.artifacts/test-runs/colink-final.KNWbAE/`、`colink-full.z5gp00/`、
  `colink-intelligence.LNIFVu/`、`colink-intelligence-fix.PoAtjb/`。
- HTTP：`.artifacts/validation/colink-0.4.2/http/20261005T150002Z-7960d79d/`。
- stdio：`.artifacts/validation/colink-0.4.2/stdio/20261005T150002Z-3469e390/`。
- 合成基准：`.artifacts/benchmarks/colink-intelligence-0nr3un_h/report.json`。
- 原生开发 app：`.artifacts/builds/colink-0.4.2-read-development/Colink.app`。

早期用例暴露相对导入匹配、符号/模块默认混选、SDK 结果类型和旧工具计数/测试字段
问题，修正后重跑；最终保守性复核又补了 receiver 遮蔽、条件重导出、重载和自身导入
循环回归。失败/中间目录全部保留，不改个人配置、凭据、数据库或真实项目源码。
这些结果不是完整安全认证、动态类型/调用全覆盖或 ChatGPT 新工具在线验证。

## 0.4.1 · 网页验收与开源预览发布

日期：2026-10-05（Asia/Shanghai）。早期本地锚点验证后，用户解锁并明确授权仅用
自带样例进行热更新测试，随后恢复原目录。本机安装版安全更新至 0.4.1，旧应用、
凭据、目录选择和镜像保留；源码运行部分与修复锚点一致，不推进写入或功能重构。

### 真实网页与原生操作

| 检查 | 实际结果 |
| --- | --- |
| 原生应用 | 0.4.1 元数据及 deep/strict 签名通过；通过 Finder 重开，实际操作原生文件夹选择、启动确认、启动与关闭 |
| 关闭状态 | 关闭样例后 running=false、ready=false、phase=stopped；不是仅收起面板 |
| 原连接刷新 | 刷新完成后，详情显示六个只读工具，包含 connection_status；现有权限设置不变 |
| 普通新对话 | 未手动选择 Skill 或 Colink，能识别样例、列出两份代码文件并读取指定标记 |
| 缺少前次 | 修改前的比较请求明确说明 NO_PREVIOUS_SNAPSHOT；没有展开整仓补丁 |
| 盲测热更新 | 仅改样例 main.py 一行，不在网页提示透露新值；网页读取新值并摘要为仅一文件修改、1 行新增/1 行删除 |
| 连接状态 | 网页返回 ready、最近同步成功、监控和过滤启用；不声称由 MCP 直接观测远程隧道 |
| 已有上下文反例 | 普通新对话提供完整短代码后直接解释，页面未出现 Colink 调用活动 |
| Skill 安装 | 编辑器保存成功，列表确认已安装；生成策略为 allow_implicit_invocation: true |
| 恢复 | 样例与原备份逐字节一致；恢复原目录与运行状态后，网页只查元信息并识别正确原项目，同步正常 |
| 保留窗口 | 样例修改及恢复后，只读查询仍为两份快照、三份唯一文本内容；不执行清理或迁移 |

热更新读取与差异摘要的整轮网页回复约 16.38 秒，包含模型处理和工具编排，不是单次
MCP 请求或同步延迟测量。现象证明不手动选技能的调用链路可用；网页未提供逐次 Skill
正文加载日志，不能证明每轮必定加载它或所有账号均有此入口。ZIP 上传导入未实测。
本次是维护者账号内安装 Skill，不等于公共插件目录发布。

首次网页保存 Skill 失败，重试一次后成功；原生辅助功能文本曾保留旧目录路径，但
截图实际目录、本机状态和网页内容均正确。两项观察保留，不因一次重试成功宣称
所有 UI 问题消失。私人原始网页记录只存本机，不上传账号/插件/Skill 标识和会话链接。

### 发布前复核

- 新独立目录全量测试：300 passed，19.17 秒，1 条原有 Starlette/AnyIO 弃用提示。
- Ruff check / format check 通过，32 个文件；锁定依赖同步成功，第三方依赖未升级。
- HTTP 和持续 stdio 两套端到端 demo 重新执行通过，均发现六个只读工具。
- 既有 0.4.1 包的 SHA-256、deep/strict ad-hoc 签名及 DMG 镜像复核通过。
- 对发行 bundle 的常见泄露检查：1804 files / 0 findings；这不是完整 DLP 或安全认证。
- 发布 DMG、ZIP、通用 Skill 备用 ZIP 和 SHA256SUMS；不发布私有配置、数据库、
  缓存、旧应用备份或原始网页记录。原 0.4.0 和大改前锚点不覆盖。

仍为 Apple Silicon / macOS 14+ 开源预览版，没有 Developer ID 签名或公证。未进行
长期、多机、任意账号、全面安全或每种工具参数组合的验收；未增加远程写入和命令执行。

## 0.4.1 · 早期本地修复与大改前锚点验证

日期：2026-10-05（Asia/Shanghai）。本轮先完成本地部分，不扩大用户目录、不改
现有连接/凭据、不覆盖正在运行的 0.4.0，不把入口可见当作网页自动匹配验收。

| 检查 | 实际结果 |
| --- | --- |
| 全量回归 | 最后一轮 300 passed，18.72 秒；前一轮 299 passed，18.78 秒；各 1 条原有 Starlette/AnyIO 弃用提示 |
| 定向回归 | 第一批相关 177 项通过；新增完整工作流回归纳入最终全量 |
| Ruff | `check .` 与 `format --check .` 通过；再按 src/tests/scripts/macos 范围复核，32 个源码/测试文件通过 |
| 锁文件 | `uv lock --offline` / `uv sync --locked`；仅 Colink 0.4.0 → 0.4.1，无第三方升级 |
| Skill 文件 | 官方 skill-creator `quick_validate.py` 通过；其 PyYAML 在独立 uv 环境，不加入项目锁文件 |
| HTTP demo | passed，6 个只读工具、完整/增量、固定重试、崩溃恢复、快照隔离及敏感文件排除 |
| 持续 stdio demo | passed，6 个只读工具、实时更新、停机编辑重启恢复、两份保留、过期拒绝，无数字 MCP 版本参数/结果 |
| 网页界面 | 原生 Chrome 观察到 Plus 账号的「插件 → 技能 → 添加技能 → 使用编辑器创建」和三个输入区；未创建 Skill |
| 网页激活/更新 | 未验证；用户保持锁屏，网页安装/启用/自然问题自动匹配与更新后的源码读取不宣称完成 |
| 当前安装版 | 元数据仍为 0.4.0，已有监督进程/私有隧道运行；本轮不停止或覆盖 |
| 新安装包 | 自包含 0.4.1 DMG/ZIP 生成；原生 arm64、最低声明 macOS 14、`LSUIElement=true`、搬移运行验证与 ad-hoc 签名通过 |
| 新包检查 | 包内后端逐文件与当前源代码一致；deep/strict 签名、DMG 镜像校验和 Publication guard 1804 files / 0 findings |
| 新包演示 | 包内 Python 用 -I/-B 运行 HTTP 与持续 stdio 两套 demo 均 passed，之后签名仍通过；不加载个人连接、不访问 OpenAI API |
| Skill ZIP | 成员仅 SKILL.md、agents/、agents/openai.yaml；核心指令与源码一致，不含私有元数据；网页上传格式/导入未实测 |

保留目录：

- 最终全量：`.artifacts/test-runs/anchor-041-final.coKrVv/`；前一轮 `.artifacts/test-runs/anchor-041.VkbY4b/`。
- HTTP：`.artifacts/demo/20261005T081913Z-1d336713/`。
- 持续 stdio：`.artifacts/demo-local/20261005T081913Z-c1d0edbd/`。
- 锚点新包：`.artifacts/releases/colink-0.4.1-anchor/`，含报告和 SHA256SUMS。
- 包内 HTTP：`.artifacts/anchor-041-bundled-validation/http/20261005T082437Z-814a4646/`。
- 包内 stdio：`.artifacts/anchor-041-bundled-validation/stdio/20261005T082438Z-47b3ab9c/`。
- Skill 备用包：`.artifacts/skills/colink-code-context-0.4.1.zip`。

新包体积为普通文件原始字节合计：应用 133880196 bytes（约 127.7 MiB），ZIP
54250854 bytes（约 51.7 MiB），DMG 68042238 bytes（约 64.9 MiB）。不是运行数据库
体积，也不是 APFS 分配块。未新做原生 UI/安装覆盖验收；签名不是 Developer ID 公证。

早期新增断言 8 个预期失败，表示缺陷复现和缺失功能断言；修复后通过。第一次全量
临时目录放在 `/private/tmp`，应用禁止 `/private` 来源，文件夹相关用例因此拒绝；
改用新的项目内临时目录，不放宽安全规则，失败目录保留。

完整核验分类见 [AUDIT_0.4.1.md](AUDIT_0.4.1.md)，锚点与后续边界见
[REFACTOR_ANCHOR.md](REFACTOR_ANCHOR.md)。本机测试不代表所有潜在 Bug 已排除、
完整安全/性能认证或每个 ChatGPT 账号都有相同入口。

## 0.4.0 · 开源、自包含安装与菜单栏启动

日期：2026-10-05（Asia/Shanghai）。发布目标为 YuyanYYt/Colink、公开、MIT，
新包声明为菜单栏 agent。维护者正在运行的 0.3.3 安装版未覆盖、未停止；本轮不读取
个人运行密钥、不修改现有 Tunnel/工作区授权，也不执行新的真实项目网页源码查询。

### 本机与发行产物实测

| 检查 | 实际结果 |
| --- | --- |
| 全量测试 | 最后一轮 288 项通过，17.73 秒，1 条原有上游 AnyIO 弃用提示 |
| Ruff / shell | 31 个 Python 文件符合格式，lint 通过；安装脚本 bash 语法通过 |
| 自包含构建 | 新目录 `.artifacts/releases/colink-0.4.0-publish/`；Apple Silicon，声明最低 macOS 14，真实构建/界面环境为 macOS 27.0.1 |
| 包内容 | 1804 个普通文件、2 个内部相对 symlink；只含运行环境、源码、样例、通用资源与许可证 |
| 重定位 | 搬移后隔离 Python 和原隧道形状的 Python 命令均可导入 backend/MCP/原生扩展；sysconfig prefix 解析到当前 bundle，不含维护者 Python 安装目录 |
| 签名 | 各原生组件和外层应用 ad-hoc 签名、deep/strict 验证通过；不是 Developer ID 签名或公证 |
| 安装包 | ZIP 与 DMG 成功生成，DMG 校验并实际只读挂载；卷内只有 Colink.app 与 Applications 快捷链接 |
| ZIP 安装 | 默认 dry-run 不安装；随后在新的独立验证目录实际解压/安装，前后应用签名均通过，没有覆盖 `/Applications` 的现有版本 |
| 发行版 HTTP 演示 | 用包内 Python、空白受控环境执行 passed，覆盖增量同步、幂等重试、恢复、隔离、搜索/差异及五个只读工具 |
| 发行版 stdio 演示 | passed，覆盖修改更新、完整重启后发现停机编辑、两份保留、过期拒绝和 MCP 无数字版本参数/结果 |
| 演示后完整性 | 两套演示执行结束后，最终 app 的 deep/strict 签名仍通过；演示数据仅在独立 `.artifacts` 子目录留存 |
| 安装预检回归 | 模拟支持的系统，只执行真实脚本 dry-run；正常 ZIP 目录尾斜杠通过，遍历/双分隔符/其他根和错误校验值拒绝，不提取文件 |
| UI | 独立 bundle ID 和用户数据名的测试副本实际打开，显示“先设置连接”；首次设置 SecureField/取消、原生文件夹选择/取消均正常，不保存身份、不发起连接 |
| Dock 声明 | 最终包 `LSUIElement=true`；测试副本实际运行时 macOS `activationPolicy=1`（accessory），原安装版仍为 regular；不是对整个系统 Dock 的修改 |
| 源码 Python 包 | wheel/sdist 构建通过，86 个归档成员审查未包含私人配置、数据、应用或构建缓存 |
| 运行依赖 | `uv sync --locked`；不升级 Python 运行依赖。开发专用 SVG 渲染器 sharp 固定 0.35.5，npm audit 返回 0 项，不等于完整应用漏洞审计 |

最终体积按普通文件原始字节合计，不代表 APFS 分配块或运行时数据目录：

- Colink.app：133870511 bytes，约 127.7 MiB。
- ZIP：54248377 bytes，约 51.7 MiB。
- DMG：68043659 bytes，约 64.9 MiB。

之前约 1.2 MiB 的开发版只是引用项目运行环境的界面壳；新包包含解释器、运行依赖
和官方客户端，所以两者不能直接比较为应用代码膨胀。

### 发布范围和边界

最终 bundle 发布检查为 1804 files / 0 findings。该检查是常见泄露规则与成员审查，
不是完整 DLP 或安全认证。原官方 tunnel-client 0.0.15 ZIP 的 SHA-256 为
`b2cae3aa9df45b4c2fe9b1d700ebacce39f9feb6a6b46b86e6499f9a51bf72ff`，
与官方发行 digest 一致；提取的程序/许可证与 ZIP 成员一致。
官方二进制内固定 EC PEM 与三个固定 example Tunnel ID 逐项按内容 SHA 归因到原官方
发行，扫描仅对该组件/规则/内容哈希豁免，不豁免其他真实身份材料，也不声称二进制
完全没有加密格式字符串。用户密钥、来源路径、镜像和私人 plugin manifest 不打包。

首次配置仅建立样例 profile 和权限 600 的本机私有密钥文件，不扫描源码、不自动
启动网络。已有配置拒绝覆盖；部分失败不自动删配置。真实运行数据不进入应用包，
退出/卸载也不清除持久化镜像。每位新用户仍需自己创建/授权 Tunnel 和 ChatGPT 连接。

本次没有重做实际网页端的代码修改后审查、四指手势、系统 Spotlight UI、跨多台
Mac 或 macOS 14 原生界面验收。GitHub 开源发布不等于 ChatGPT 公共插件目录上架。
过去的网页证据仅在以下历史章节按当时范围保留，不推广为每个新账户均已通过。

### 留存证据与早期失败

- 最终全量目录：`.artifacts/colink-final-verified-tests.1ZXVQJ/`。
- 最终包：`.artifacts/releases/colink-0.4.0-publish/`，包含报告和 SHA256SUMS。
- 发行版 HTTP：`.artifacts/colink-publish-bundled-validation/http/20261004T190733Z-0abe7a42/`。
- 发行版 stdio：`.artifacts/colink-publish-bundled-validation/stdio/20261004T190734Z-a6a749b3/`。
- 安装验证：`.artifacts/colink-publish-install-check.tkteqf/`；安装暂存目录也保留。
- UI 测试副本：`.artifacts/colink-ui-check/`；退出后确认只剩原安装版进程。

首次原生构建遇到当前 SDK 的 SwiftUI State 宏不可用，改用 ObservableObject 后构建
及真实 UI 通过，没有修改全局工具链。首次独立包遇到 Python dylib 的绝对安装标识，
在新副本签名前改为内部 rpath；随后移除 sysconfig 中的构建机前缀并以当前位置重定位。
实际 ZIP 安装暴露正常目录尾斜杠误判，已修正并补拒绝异常路径的回归。首次完整
stdio 演示的 SDK 子进程未继承父进程禁缓存设置，已在演示子进程 argv 增加 `-B`；
新的最终包执行完整演示后签名有效。所有失败/中间包均保留，不能当作最终发行包使用。

中途一轮全量出现关闭用例退出码 1，另 286 项通过；独立复核三种关闭方式通过。
源码复核发现“进程组恰好在探测与发信号之间退出”未捕获的竞态，已修正并用确定性
模拟验证仍回收 leader、检查后代。原失败未保存子进程 stderr，不能断言它已确认
就是同一原因；测试现已在退出失败时收集 stderr，最后一轮全量 288 项通过。

本轮不自动删除构建、失败、下载或暂存目录。原始个人记录另存于未提交的私有备份，
不是可随意清理的缓存；密钥、源码、持久化数据库和原安装版均保留。

## 0.3.3 · Colink 与两份代码状态

日期：2026-10-05（Asia/Shanghai）。本轮同时处理对外命名与历史存储，不增加可读
目录、远程命令或源码写入能力。旧章节按当时证据保留，不把历史版本名称改写成新名称。

### 实际安装与网页验证

| 检查 | 实际结果 |
| --- | --- |
| 对外命名 | 项目发行名 `colink-mcp`、CLI `colink`、MCP 初始化服务名、原生面板/菜单/应用元数据均为 Colink；原 `code-context` 命令仍可用 |
| 兼容边界 | bundle ID `local.codeconnect.menubar`、连接/版本 ID、模块与数据路径不变，原文件夹偏好保留；维护不查看或改写密钥，原有 launcher 仍按原机制加载运行凭据 |
| 依赖 | `uv sync --locked` 成功，43 个包；只更换本地项目包名/维护版本，不升级依赖 |
| 全量回归 | 最后一轮 220 项通过，16.37 秒；前一轮 219 项通过，15.65 秒；各一条原有上游 AnyIO 弃用提示 |
| Ruff | `check src tests macos/build.py` 与 `format --check` 均通过，25 个文件符合格式 |
| 本机端到端 | HTTP 同步/官方 SDK 演示 passed；持续 stdio 演示 passed，验证实时更新、停机编辑恢复、两份保留与过期拒绝 |
| 原生构建 | `dist/releases/0.3.3-final/Colink.app` 编译及 ad-hoc 签名通过，复用根目录 Swift 编译缓存 |
| 安装与签名 | `/Applications/Colink.app` 与项目根 `Colink.app` 均通过 `codesign --verify --deep --strict`，应用本体各约 1.2 MiB，不含引用的运行依赖 |
| 原生界面 | 实际截图显示 Colink、原有 SVG 标志与毛玻璃面板；重开先关闭，保留更新前已选择的来源 |
| 同来源恢复 | 旧连接先关闭、监督锁释放；新版手动恢复同一来源后 `supervised/running/ready=true`，29 个文件，安装版进程 55332；没有选择新目录 |
| 系统元数据 | Spotlight 元数据为 `Colink.app` / `local.codeconnect.menubar` / 0.3.3；不是重启电脑或系统 Apps 手势实测 |
| 网页名称 | 原生 Chrome 将现有插件显示名称和连接昵称从 Code Connect 保存为 Colink；详情页、连接卡片与新聊天标签均核对成功 |
| 网页工具刷新 | 在原连接管理页刷新工具，详情实际显示 Read5 及新的 current/previous、内部标识、无数字版本说明 |
| 网页实际查询 | 本地保存的实际对话通过 Colink 查询项目元数据，回复“1 个项目、29 个代码文件”，与本机数量一致，无版本编号；私人对话链接不公开 |

网页插件及版本 ID 保持不变，个人标识不在公开副本展示。更新前界面已有的“允许使用所有工具”
选项保持不变；没有改授权选项、新建连接、上传插件包或修改组织/工作区。
本轮网页仅查询元数据，没有读取真实项目源码，也没有进行新的“修改真实源码后网页
审查”验收。原始网页工具 JSON 未单独导出，不把本机 ready 或演示当作这类网页证据。
网页图标显示问题仍按用户要求停止处理；SVG 只改标题，不改形状。

### 历史保留、实测与实际数据迁移

每个项目按完整代码状态保留当前与前一次；未变化文件共享内容，不按对话轮数存新
历史。成功提交原子删除更早清单/快照/幂等记录及全库无引用源码，其他项目仍引用的
内容保留。内部同步编号继续单调递增，但不进入五个 MCP 工具的参数和结果。

新增定向测试覆盖 300 次小改、共享内容、重试/冲突、清理失败回滚、并发读写隔离、
比较事务、过期标识、旧 schema 迁移与压缩中断恢复。全量检查包含新命名的 stdio
初始化及原生品牌/偏好兼容性保护。

| 存储场景 | 实际结果 |
| --- | --- |
| 约 100 KiB 文件单行改动 300 次 | 快照/文件清单/内容/重试记录各只保留 2 条；源码 204826 bytes，数据库 368640 bytes（360 KiB） |
| 旧 schema 20 份约 100 KiB 历史迁移 | 只留最近 2 份，数据库压缩到 270336 bytes（264 KiB）；测试同时断言小于升级前的一半 |
| 压缩中断后重启 | 保留已生成内部标识，继续压缩到 264 KiB，待处理标记清除；不默默遗漏磁盘回收 |
| 现有 3 个镜像数据库 | 安全停止旧应用后全部升级至 schema 2、增量回收开启，待压缩标记均为空；没有删除数据库文件 |
| 原样例持续镜像 | 从 3 份降为最近 2 份，唯一内容从 823 降为 680 bytes，仅移除较早派生历史 |
| 其他 2 个既有镜像 | 各保留原来的 1 份；当前来源 29 个文件未改变，不为验证修改源文件 |

实际迁移路径仅 `.code-context/server/mirror.sqlite3`、
`.code-context/local-sample/server/mirror.sqlite3`，以及已登记来源
`.code-context/desktop/sources/<source-hash>/data/server/mirror.sqlite3`（个人来源标识已脱敏）。
小数据库新增索引/标识/维护表后，物理文件分别为 64 / 64 / 160 KiB；本轮并不宣称这些
原本很小的文件已缩小。解决的是历史文本随编辑次数累积，整目录大小仍包括客户端
状态、WAL/SHM、索引与空闲页。每份来源最多 8 MiB 源码，两份正文最多 16 MiB，
不是整个项目/数据库目录的硬上限，多个来源也分别计数。

带旧 revision 参数的缓存工具调用明确拒绝并要求刷新，不静默读取错误状态。
过期标识需要从新概览重新分析；不会为了继续旧对话而恢复已清除的无限历史。

### 留存、早期失败和可选清理

- 最后一轮全量：`.artifacts/colink-tests-final.u4NHDS/`。
- 前一轮全量：`.artifacts/colink-tests.fYu8c9/`。
- HTTP 演示：`.artifacts/colink-validation/http/20261004T174149Z-2b8e9ab5/result.json`。
- 持续 stdio 演示：`.artifacts/colink-validation/stdio/20261004T174150Z-9239aef1/result.json`。
- 最终构建：`dist/releases/0.3.3-final/Colink.app`；首轮构建 `dist/releases/0.3.3/Colink.app` 保留。
- 更新前安装版与根目录副本移入 `dist/archive/colink-rename-20261005/installed/`、`project/`，可恢复，没有删除。
- `colink-mcp` wheel/sdist 构建通过，成员检查未包含密钥、`.code-context`、`.artifacts` 或应用 bundle。

初轮 56 项通过、1 项失败发现 auto_vacuum 与 WAL 的设置顺序问题，已修正并增加实际
数据库属性断言。独立复跑 60 项通过；曾在系统临时目录跑全量得到 210 通过/9 失败，
原因是原有 `/private` 来源选择保护拒绝测试路径。没有放宽保护，改用工作区内新的
独立测试目录后全量通过。初期测试与失败目录全部保留：
`/tmp/code-connect-retention-unit.iVcPWo`、`/tmp/code-connect-retention-retry.Ok4yMx`、
`/tmp/code-connect-retention-full.c4FEcw`、`/tmp/colink-desktop-check.U7Ey84`。

按用户规则不自动删除生成物。可考虑在确认后清理前一轮全量目录
`.artifacts/colink-tests.fYu8c9/`（约 5.4 MiB）与首轮构建
`dist/releases/0.3.3/`（约 1.2 MiB）；最终测试、最终构建、原应用归档与失败证据保留。
这些不属于已授权的运行时两份快照清理，当前没有删除。

## 0.3.2 · 网页命名与严格中心对称标志

2026-10-04 在用户的原生 Chrome 中管理现有 ChatGPT 插件，将显示名称从
`Code Context Sample` 保存为 `Code Connect`。随后查看插件详情，并点击“在聊天中试用”，
实际聊天输入框的插件标签为 `Code Connect`，没有发送新的聊天消息。

- 插件及版本 ID 保持不变，公开副本不展示个人标识。
- 网页插件版本仍为 1.0.0，
  与桌面应用版本分别维护，本次只是重命名现有连接。
- 仍为“允许使用低风险工具”、无需额外 MCP 身份验证；未重建插件或修改权限。
- 私有 Tunnel 的控制面内部名称仍为 `Code Context Sample`，现有运行配置未改。
- 本轮只修改网页名称，不宣称远端插件图标也已更新；SVG 修改应用于原生桌面应用。

### 图标与安装检查

| 检查 | 实际结果 |
| --- | --- |
| 画布和图形中心 | 原链条中心 `(128, 130)` 改为 `(128, 128)`；9 段路径的对应控制点严格中心对称 |
| 括号 | 左侧外移 11 个源坐标单位，右侧由 `rotate(180 128 128)` 生成，主标志/菜单栏几何一致 |
| 间隙 | 左右相等；曲线采样减去导数误差上界和笔画半径后，实际间隙仍大于 6 个源单位 |
| 颜色和底板 | 中心径向底板，镜像渐变色标；颜色设计也保持 180° 对称 |
| PNG 栅格检查 | 256 px 主图与 46 px 菜单图的旋转像素平均通道差分别约 0.041 / 0.050（0–255） |
| 构建与签名 | `dist/releases/0.3.2/Code Connect.app` 编译和 ad-hoc 签名验证通过 |
| 安装 | `/Applications/Code Connect.app` 和项目根副本均为 0.3.2，内置 SVG 哈希与源文件一致 |
| 原生界面 | 实际打开新版毛玻璃面板，检查更新标志与样例文件夹；连接显示“已关闭” |
| 系统元数据 | 安装版 Spotlight bundle ID 与版本读取为 `local.codeconnect.menubar` / 0.3.2 |
| 完整回归 | 最后一轮 207 项通过，15.01 秒；第一轮亦为 207 项通过，14.28 秒；一条原有上游弃用提示 |
| 规范与演示 | Ruff check / format check 通过；持续 stdio 和 HTTP 两套 demo 均 passed |

这里的“严格中心对称”指 SVG 几何和颜色定义；PNG 在曲线边缘存在渲染器抗锯齿取整差，
不宣称导出的 PNG 每个像素完全相同。菜单栏和应用图标均从更新后的 SVG 派生。
旧安装进程 39303 已退出，安装后新进程 41176 来自 `/Applications/Code Connect.app`。
只改显示资源和版本，不改监督、同步、存储、五个只读工具、凭据或自动启动行为。

两轮全量通过之间，`branding-20261004-05` 的原有
`test_supervisor_shutdown_stops_entire_tree_and_stays_closed[signal]` 出现一次退出码 1
（其余 206 项通过），失败目录的 runtime 记录停在 `stopping`。测试没有保存该子进程
stderr，因此尚未定因，不能宣称这个偶发问题已修复。失败中的样例 MCP PID 已不存在；
随后独立桌面测试 10 项通过，最后全量 207 项通过。此轮保持运行代码不变，保留失败
证据，关闭时序问题应另行定向排查，不用复跑成功掩盖失败。

本轮保留产物：

- 全量通过：`.artifacts/tests/branding-20261004-03/`、`branding-20261004-07/`。
- 关闭测试偶发失败：`.artifacts/tests/branding-20261004-05/`；独立桌面复跑：`branding-20261004-06/`。
- 标志定向测试：`branding-20261004-04/`；全部早期失败与验证目录均保留。
- 持续 stdio 演示：`.artifacts/demo-local/20261004T152442Z-b7e1015c/result.json`。
- HTTP 演示：`.artifacts/demo/20261004T152442Z-8d8570fb/result.json`。
- 0.3.1 安装版及根目录副本备份：`dist/archive/0.3.1-branding-20261004/`。
- wheel/sdist 成员检查未包含 `.env.local`、`.code-context`、`.artifacts` 或应用 bundle。

结束时应用打开但连接关闭，sample/revision 3/2 个文件仍保留，没有新采集或新增目录。
本轮没有重新进行网页源码调用；0.3.0 的真实网页读取证据保留在下方，不用本机测试替代。

## 0.3.1 · 标志、面板与系统打开入口

2026-10-04 在当前 Mac 上完成本轮定向修正。只改变原生呈现、应用元数据和打开入口，
实际监督、上传、存储、查询与只读权限边界不变；未更换密钥或扩大共享目录。

| 检查 | 实际结果 |
| --- | --- |
| SVG | 连接链合并为单条连续路径；9 段曲线/直线的相接切向同向；主标志与菜单栏共用几何 |
| 原生面板 | 实际截图检查关闭/连接状态；技术标签、快照/文件数、常驻共享说明已移除 |
| 构建 | `dist/releases/0.3.1-final/Code Connect.app` 编译及本地签名验证成功 |
| 安装 | `/Applications/Code Connect.app`；项目根目录保留同版副本 |
| Finder 打开 | 实际从“应用程序”定位图标、⌘ O 打开；进程路径来自安装目录 |
| 退出后重开 | 原进程 38955 退出，Finder 再开得到 39303；连接保持关闭 |
| 手动启动/关闭 | 样例连接实际 ready/running 为真；点击关闭后均为假，监督锁释放 |
| Spotlight 索引 | `mdfind -onlyin /Applications` 返回唯一安装路径；应用 bundle 元数据可读取 |
| 自动化回归 | 204 项连续两轮通过，14.79 / 14.94 秒；原有一条上游弃用提示 |
| 代码规范 | Ruff check / format check 通过，24 个文件符合格式 |
| 本机端到端 | stdio 持续演示、HTTP 同步/查询演示均 passed |

本轮没有重启用户电脑，也没有实际模拟触控板手势。系统 Apps/Spotlight 窗口在 Computer
Use 中未能读取，不能把安装/索引证据当成启动器界面实测；Finder 标准打开操作有实际
操作与新进程证据。未改系统手势、搜索快捷键、登录项、Dock 固定项或安全设置。

前期回归暴露两处测试时序问题：模拟客户端在收到进程组 SIGINT 后立刻对正在退出的
子进程补发 SIGTERM，留下测试数据库热日志；另一个等待函数只等写锁与旧 revision，
可能早于重启对账完成。已修正模拟客户端的有界等待/关闭和等待真实镜像 ready 的条件，
没有放宽关闭、进程消失、旧快照或重启更新的原断言，也没有修改实际后端行为。
失败运行目录保留，最终两轮通过分别为：

- `.artifacts/tests/macos-polish-20261004-04/`
- `.artifacts/tests/macos-polish-20261004-05/`

本轮 demo：

- `.artifacts/demo-local/20261004T150156Z-e119da68/result.json`
- `.artifacts/demo/20261004T150158Z-06a92e5b/result.json`

旧根目录 0.3.0 应用保存在 `dist/archive/0.3.0-project-20261004/Code Connect.app`；
预览安装与项目副本在 `dist/archive/0.3.1-preview-20261004/`。本轮所有构建、编译缓存、
测试与持久化镜像均保留，不自动删除。结束时只选原样例，revision 3 保留，应用打开而
连接关闭，需要用户手动启动。网页能力的此前实际证据见下方 0.3.0，未将本轮本机检查
冒充新的网页调用。

## 0.3.0 · Code Connect 原生菜单栏应用

日期：2026-10-04（Asia/Shanghai）；实际环境：macOS 27.0.1、Apple Silicon、Swift 6.4、
Python 3.11.15、官方 MCP SDK 2.3.0、官方 tunnel-client 0.0.15。

### 构建、测试与实际界面

| 检查 | 实际结果 |
| --- | --- |
| 依赖 | `uv sync --locked` 成功；仅包版本升为 0.3.0，不升级 MCP SDK |
| 完整测试 | 200 项通过，12.54 秒；一条原有上游弃用提示 |
| Ruff | `check` 与 `format --check` 覆盖 src、tests、macos/build.py，均通过 |
| 原生构建 | 项目根目录 `Code Connect.app` 编译成功，约 1.2 MiB |
| 签名检查 | 本机 ad-hoc 签名，`codesign --verify --strict` 通过 |
| 原生 UI | 通过 Computer Use 查看实际 AppKit/SwiftUI 菜单栏弹窗与截图，非网页 mock |
| 系统文件夹选择 | 原生选择窗口实际打开，确认 sample_project 后仍显示关闭，没有自动采集 |
| 启动按钮 | 实际点击后由应用 PID 托管监督进程，镜像 running/ready 与隧道健康均通过 |
| 关闭按钮 | 实际点击后 running/supervised/ready 均为假，采集锁释放，客户端进程消失 |
| 退出并重开 | 应用进程更换后仍为关闭，启动按钮可用，没有恢复运行意图 |
| 原有两套演示 | 持续 stdio 和 HTTP 同步/SDK 演示均通过 |

新增 10 项桌面生命周期/来源测试，覆盖只读检查无创建副作用、选择不扫描、来源隔离、
过宽目录/符号链接拒绝、外部连接只报告不杀进程、管道 EOF（模拟退出/崩溃）、关闭请求、
SIGTERM、重复连接拒绝、停机修改的手动重启恢复，以及历史来源移动后的状态检查。
真实界面使用原生毛玻璃和动态主题颜色；当前系统深色主题已截图检查，未擅自切换
全局系统主题。SVG 为 logo 源，构建派生 PNG/ICNS，原始 SVG 保留。

### 菜单栏托管后的实际网页验证

保持原有样例范围和私有 Tunnel，不更改组织/工作区权限。由应用启动连接后，本地
新增 `NATIVE_APP_CHECK` 验证常量，其值与哈希未告诉网页模型，再发起实际只读查询。
本地保存的实际验证对话返回（私人链接不公开）：

- 最新 revision：`3`，本机界面同步显示 `r3`、2 个文件。
- `NATIVE_APP_CHECK`：`codeconnect-49e71b6a-20261004`，位于 `main.py` 第 5 行。
- SHA-256：`6cdf21356723f4c2d204e46fcf34a17db929f9e3cbe4ab0dbbd16f10eae8f645`，与本机一致。
- 固定 `revision=1` 再次读取成功，仍不包含该常量。

网页版内部原始工具 JSON 未单独导出；证据为实际界面返回、未在提示中提供的新标记、
本机文件哈希与真实镜像状态的联合核对。不将本机状态中的 `chatgpt_web_verified=false`
当作网页失败，也不将本机 ready 当作网页调用成功。

### 本轮留存与最终状态

- 最终应用：`Code Connect.app`。
- 最终完整测试：`.artifacts/tests/desktop-20261004-02/`。
- 持续 stdio 演示：`.artifacts/demo-local/20261004T142015Z-55734d2c/result.json`。
- HTTP 演示：`.artifacts/demo/20261004T142017Z-6186846d/result.json`。
- SVG：`macos/assets/logo.svg`、`macos/assets/menubar.svg`。
- 早期应用：`dist/Code Connect.app`、`dist/releases/0.3.0/Code Connect.app`，均保留。

本次验收结束时，原生应用已打开，连接有意保持关闭；`supervised/running/ready` 均为假，
健康监听已退出，原采集进程消失，revision 3 及旧快照仍保留。需要用户点击启动才会连接。
没有设置登录自启、修改睡眠/系统安全选项、连接真实目录或自动清理任何产物。

## 0.2.0 · 本机托管与实际 ChatGPT 网页连接

日期：2026-10-04；环境：macOS Apple Silicon、Python 3.11.15、官方 MCP SDK 2.3.0、
官方 tunnel-client 0.0.15。访问范围仅项目内的 `examples/sample_project`。

### 本机代码与协议验证

| 检查 | 实际结果 |
| --- | --- |
| `uv sync --locked` / `code-context --version` | 依赖锁定安装成功，版本 0.2.0 |
| `ruff check src tests` / `ruff format --check src tests` | 均通过 |
| 完整测试 | 190 项通过，约 7.2 秒；一条上游弃用提示 |
| `code-context demo-local` | 官方 SDK stdio 调用、实时更新、固定旧版本、完整进程重启恢复均通过 |
| `code-context demo` | 原有 HTTP 同步、SDK 查询、崩溃恢复和幂等重试均通过 |
| 官方客户端校验 | 下载 SHA-256 与官方 GitHub 发行资产一致；实际二进制版本 0.0.15 |
| 官方 `doctor` | 配置与本地检查通过；不作为控制面权限或网页调用证据 |
| `code-context tunnel-status` | 本机镜像 running/ready、health/ready 响应均为真，pending 为假 |
| `uv build` | wheel 与源代码包构建成功；包成员列表未包含 `.env.local`、`.code-context` 或 `.artifacts` |

持续 stdio 演示在独立数据目录中经过 revision 1 → 2 → 3；确认停止期间修改在重启时
对账恢复。测试还覆盖本地确认丢失后重放、版本冲突保留队列、来源绑定/独占锁、扫描
异常不误删、监听失败阻止读取、状态无创建副作用与安全 profile/凭据解析。

### 实际网页与私有连接证据

下列结果通过原生 Chrome 操作用户实际登录的 ChatGPT 网页获得，不是本机 demo 推断。
用户逐项授权创建一个私有隧道与样例连接；没有关联其他组织、工作区或代码目录。

- Platform Tunnel：`Code Context Sample`；真实 ID 仅保留在本机私有记录。
- 关联一个经界面确认的 Personal 组织和一个 ChatGPT 工作区。
- 实际插件页面（个人连接 URL 不公开）
  显示“已连接”；工具详情显示 `Read 5`，名称与预期的五个工具全部一致。
- 实际验证对话（私人链接不公开）
  已先请求 `list_projects` → `repo_overview` → 固定 revision 的 `read_file`，返回真实样例源码。
- 由本地编辑保留的 `SAMPLE_WEB_CHECK` 标记，未将其值或哈希告诉网页模型，再查询时
  网页返回精确标记、SHA-256、revision、diff 和搜索行号，并与本机逐项核对。

| 网页场景 | 核对结果 |
| --- | --- |
| 首次读取 `main.py` | revision 1，143 bytes、5 行，源码与本机原始文件一致 |
| 修改样例后查询最新版本 | revision 2，新标记值 `sample-6b3e91d2-20261004` |
| 固定旧版本读取 | revision 1 仍无标记，原始内容与哈希不变 |
| `get_diff` 1 → 2 | 仅 `main.py` 新增注释、常量和空行 |
| `search_code` 固定 revision 2 | `SAMPLE_WEB_CHECK` 命中 `main.py` 第 4 行、第 1 列 |
| 最终 0.2.0 进程完整重启后重新查询 | revision 2、标记与哈希一致，revision 1 再次读取成功 |

原始 SHA-256：`5a72013faf0468c6355625ede94fd0ed6c0dca5cf92faa0cdc7dd7d769bdada7`。
更新 SHA-256：`773972e383399143823df531979df123e33335f8c25284bedd3d5428b1873b52`。

这些网页结果与本机哈希、未知验证标记及镜像状态共同构成端到端证据；未单独导出
网页内部的原始工具调用 JSON。`tunnel-status` 的 `chatgpt_web_verified=false` 表示该
本机状态命令本身不验证网页，不否定上述独立网页实证。

### 留存产物与边界

- 最终完整测试目录：`.artifacts/tests/local-bridge-20261004-03/`。
- 持续 stdio 演示：`.artifacts/demo-local/20261004T132833Z-79d7d09e/result.json`。
- 原有 HTTP 演示：`.artifacts/demo/20261004T132834Z-67a4e9d4/result.json`。
- 首次 0.2.0 构建：`.artifacts/dist/0.2.0-verified/`；含最终文档的交付包保存在
  `.artifacts/dist/0.2.0-final/`。
- 当前持续镜像：`.code-context/local-sample/`；有效配置 `.code-context/tunnel/profile.yaml`。
- 官方下载、解压、早期 `.json` 配置与 schema 检查产物均保留，未自动清理。

本次未部署公网 HTTPS/OAuth，未使用真实项目，未安装登录自动启动或改动系统睡眠设置。
隧道是本机向官方服务发起的出站连接，没有新增本机公网入站服务。网页读取的源码会
传给 OpenAI，仍需遵守授权范围；私有连接并不等于完全离线或系统级隔离。

## 0.1.0 · 历史本机验证

日期：2026-10-04；运行环境：macOS、Python 3.11.15、官方 MCP SDK 2.3.0。

## 已执行

| 检查 | 实际结果 |
| --- | --- |
| `uv sync --locked` | 固定版本依赖安装成功 |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过 |
| 完整测试 | 155 项通过，耗时约 3.8 秒 |
| `code-context scan` | 读取样例的两个文本文件、返回 SHA-256 与大小 |
| `code-context snapshot` | 样例项目在本地建立 revision=1 |
| `code-context demo` | 本机 HTTP 与官方 SDK 完整调用通过 |
| `uv build` | 成功生成 wheel 和源代码包 |

测试使用真实文件系统事件验证运行中修改、新增与删除；通过真实 HTTP 服务模拟确认
丢失与客户端重启，验证服务端版本不重复增加；通过 stdio 子进程验证工具发现和读取。
过滤、符号链接、路径越界、哈希、容量、异常读取、认证、非法 JSON、版本冲突和回滚
均有相应测试。MCP 查询返回固定 revision 的源代码，历史快照在更新后仍可读取。

上游 Starlette 测试客户端有一条 AnyIO 别名弃用提示；测试未失败，运行演示正常。

## 留存证据

- 完整测试数据：`.artifacts/tests/release-03/`
- 独立演示：`.artifacts/demo/20261004T092953Z-7555d965/result.json`
- 最终发行包：`.artifacts/dist/0.1.0-verified/`
- 可供本地 stdio 查询的样例镜像：`.code-context/server/mirror.sqlite3`

0.1.0 的测试、早期验证目录和构建产物均保留。当时未进行公网部署、HTTPS 服务联调、
用户真实项目同步或 ChatGPT 网页 OAuth 连接；这些不属于以上通过结果。
