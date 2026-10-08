# 终端能力综合对比：Codex 固定源码、CoLink b3 与 b4 首版

本项目首版继续采用 **CoLink 自有协调器、SRT 原生沙盒基础与 Codex 执行机制借鉴**。这个选择主要由使用场景决定：用户在 ChatGPT 网页会话中开发本机项目，需要跨工具调用找回作业、保护原文件、保留写入恢复记录，并限制项目权限、缓存和服务端口。Codex 的进程、PTY 和输出模块值得复用，但完整接入 Codex 执行服务仍要补齐这些 CoLink 业务边界。目前没有同一工作负载下的 Codex 性能对照，不能据此判定哪种语言或后端更快。

当前方案的代价同样明确：CoLink 承担原生进程监督、磁盘恢复、工具链适配和数据库权限检查的维护工作；交互终端尚未提供，服务兼容性受到 Unix socket 接入方式限制。对于通用桌面交互终端、未经适配的 Spring TCP 服务或跨平台执行需求，当前首版还不足以替代完整终端环境。

## 比较对象与证据范围

本文记录于 2026-10-07，比较三个明确对象：

| 对象 | 范围 | 可以据此得出的结论 |
| --- | --- | --- |
| Codex | 固定提交 `d27764b82f7118f674371e6d6e76271d9d606edb`，对应本轮选型采用的 0.160.1 源码；重点阅读执行、PTY、Seatbelt 和相关依赖 | 模块机制与接入成本的源码结论。没有运行同负载 Codex 后端，也没有完成 Codex 整体安全审计 |
| CoLink 0.5.0b3 | 既有原文件直读、多项目与默认关闭的受控写入；基线提交 `4e5141c` | 原有网页 MCP、项目身份、写任务和恢复语义；终端执行与本地 Git 提交在该基线仍未实现 |
| CoLink 0.5.0b4 首版 | 当前源码中的执行协调器、原生监督、缓存、端口、环境与数据库模块，以及保留的本机证据 | 已实现机制与指定本机 fixture 的实际结果；不把源码存在、模拟测试或本机协议通过等同于网页与安装版验收 |

**当前状态：指定本机原生隔离、正式协调器、PostgreSQL/pgvector、MySQL 13 项与生命周期 33 项通过；真实 ChatGPT 网页完成双服务、报错修复、页面 CRUD/向量、刷新找回、幂等重试、受控移动、仅选定文件本地提交，以及服务/数据库重启后读取原数据。最终全量 2348 项、包内 32 项通过。0.5.0b4/build 10 已覆盖安装，严格签名和实际连接通过，新增安装占用约 15.80 MiB，满足 250 MiB 上限。用户已要求结束新增验收并清理所有已确认测试材料和旧应用。** 详细结果与证据边界见 [最终验收记录](../docs/TERMINAL_VALIDATION_0.5.0b4.md)。

证据分为源码实现、协议/模拟验证、原生 fixture、真实网页和最终安装五层。下面出现的“原生通过”仅限列出的机器、配置、工具链与攻击/兼容用例；未测试的系统版本、框架或项目仍须单独验证。

## 能力与适配成本对比

| 维度 | Codex 固定源码 | 原 CoLink b3 | 当前 CoLink b4 与实际权衡 |
| --- | --- | --- | --- |
| 网页会话与作业 | 执行管理器已有会话句柄、等待、输出与终止机制；这些模块本身不会自动提供 CoLink 的远程作业契约 | 网页远程 MCP 与项目路由已有，缺少持续作业 | HTTP 请求与作业分离；网页实际完成依赖/迁移、双服务与业务页面；刷新找回同一作业，取消后端后前端保留，重启读回原数据 |
| 项目授权 | 沙盒、工作区与执行授权随配置组合 | 已有 `project_id`、来源身份、本机写入授权与恢复记录 | 延续这些边界，增加终端单独授权和权限 epoch；输入、配置、工具链、网络与端口绑定计划。更贴合本项目，但规则由 CoLink 自行维护 |
| 原文件修改 | 通用执行工作区权限可配置；不能直接推导出 CoLink 写入恢复语义 | WriteCoordinator 受控修改原文件 | 命令在派生任务盘运行，生成结果经来源哈希检查和既有写入协调器写回；执行并发与原文件写回串行分别管理 |
| 取消与后代 | 独立进程组、终止确认、读取等待与执行截止时间分离 | 没有终端进程生命周期 | 借鉴基础机制后，增加 macOS 原生资源组身份、后代收束与重启证明；本机范围更窄，平台维护成本更高 |
| 输入与 PTY | 有管道、PTY、终端尺寸及输入处理模块 | 无执行入口 | 首版仅非交互管道；开发 Shell 可以运行脚本，但不是交互终端。PTY、网页输入和尺寸调整是明显差距 |
| 输出 | 有界保留开头/结尾，记录中间省略量 | 无终端输出 | 有界滚动输出、游标读取、明确省略与回收状态；小回执持久化，完整输出正文不保证跨重启保留 |
| 依赖和构建缓存 | 本次审阅的执行模块不构成 CoLink 的项目配额和引用管理器 | 无执行缓存契约 | 有工具链/锁文件/配置键、活动引用保护、项目与全局配额；npm、venv、Maven 使用方式分别适配，不能简单视为所有工具的透明缓存 |
| 服务端口 | 配置型原生网络策略；不能直接替代 CoLink 注册端口和本机确认流程 | 无受控服务启动与端口释放 | 可信主控监听注册的回环 TCP 端口，项目监听固定 Unix socket；避免项目任意绑定，但 Python/Java 服务需要适配 |
| Python/npm/Maven | 通用 Shell/执行与 PTY 可运行开发工具，具体环境取决于配置 | 可以读写这些项目源码，不能代为运行 | 同期覆盖安装、测试、构建与沙盒 Shell；已有真实依赖、Node/Vite、Python 和 Java Unix 服务证据，通用 Spring TCP 仍受限制 |
| 环境与数据库 | 通用环境/配置模块；本文未审阅出与 CoLink 相同的项目数据库引导与角色证明契约 | 没有执行前环境与 DB 验证入口 | 返回实际工具、虚拟环境创建方式和连接证明状态；PG/pgvector 与 MySQL 独立原生已验，网页人工迁移已运行，Qdrant 仅配置 |
| 体积与性能 | 窄 PTY crate 与完整执行服务的依赖成本明显不同；未作本项目实测 | 已安装应用基线 | 首版不引入 Rust 构建链、复用本机工具；最终包新增约 15.80 MiB，通过 250 MiB 上限；性能计时只描述一次 CoLink 人工流程，没有 Codex 对照 |
| 维护与许可 | 成熟模块可减少进程/PTY 自研，但整体后端依赖面较大 | 既有 Python/Swift 与写入协议 | 维持一套后端，减少双实现分歧；仍需跟踪 SRT、Seatbelt 和原生 API。借鉴源码保留 Apache-2.0 及 NOTICE |

Codex 会话与输入机制依据 [process_manager.rs](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/core/src/unified_exec/process_manager.rs)、[PTY 模块接口](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/utils/pty/src/lib.rs)；CoLink 当前接入依据 [执行工具](../src/code_context/execution_tools.py)、[协调器](../src/code_context/execution_coordinator.py)和[架构文档](../docs/ARCHITECTURE.md)。表中的未审阅范围不代表 Codex 整个产品缺少相应能力。

## 网页会话为何需要独立协调器

执行链路为 **ChatGPT 网页 → 现有远程 MCP → CoLink 作业协调器 → 原生沙盒中的开发进程 → 输出与状态 → 网页分析和修复**。网页模型负责理解报错、选择修改和重新验证；网页停止调用后，本机不会另行调用模型继续修复。

`execution_plan`、`execution_rehearse`、`execution_start`、`execution_status`、`execution_output`、`execution_cancel` 与 `execution_list` 使用明确的项目、计划和作业标识。启动先保存幂等绑定，再异步准备任务盘、输入和运行环境，接口最多等约 2 秒收集初始结果；进程不会随一次 HTTP 请求结束而终止。来源或权限变化后，旧计划失效；重试相同请求不会再启动第二个进程。查询优先展示仍运行或尚未确认收束的作业，避免大量旧回执遮住当前服务。[协调器](../src/code_context/execution_coordinator.py)、[执行账本](../src/code_context/execution_store.py)

预演安装或生成器会真实执行隔离任务。正式启动复用该结果，生成内容只在正式写回阶段应用一次；普通测试/构建无需重复运行。`process_alive`、端口可连接、应用健康检查和实际退出结果分别表达，命令已启动不能替代“应用可用”的证明。工具的副作用注解与这些行为相符；网页实际调用与重连后的作业记录已有第三轮证据，客户端动作确认行为仍由 ChatGPT 决定；双服务、业务页面与实际刷新找回已按各自证据验证。

实际网页使用 b4/build 10 源码候选，完成私有 venv/six、npm、PostgreSQL migration/pgvector 与故意错误退出。重连保留原六作业 ID 并不自动重跑；受控修复后启动 43117/43118 双服务，真实 Chrome 页面写入/读取记录和三维向量。刷新 ChatGPT 后找回原服务，重复 start 不重复进程；只取消后端并重启后，前端保留原 ID，页面仍读到原数据。随后网页受控创建/移动说明、仅提交该文件并保留 requirements 原暂存，有限 SQL 完成 CRUD。人工 PostgreSQL 正常重启后，真实页面再次读回原记录和三向量。最终自包含包的 32 项及安装连接验证另行记录，不能把源码候选网页结果混称为最终包全部网页验收。20 分钟完整网页闲置等待未追加执行。[真实网页与安装记录](../docs/TERMINAL_VALIDATION_0.5.0b4.md)

网页也实际触发了第六次启动被开发任务预算拒绝，模型随后取得新的 `development_task_id`。因此“五轮/二十分钟”只约束已绑定的服务端任务 ID；它不能识别跨新任务 ID 的同一个自然语言目标，不能将该机制表述为任意修复目标全程只允许五次尝试。

账本与输出有不同寿命。小回执按 24 小时保留，执行数据总预算 32 MiB，并为 SQLite 回滚日志留出约半数预算；正文采用内存有界滚动缓冲。最终读取确认、到期、管理器淘汰或重启后，接口会明确返回输出已回收及终态回执，不能许诺恢复全部历史日志。账本维护只回收已过期、原生清理已验证、任务盘已退休且没有写回/运行引用的终态记录；未知、停止失败和待写回记录保留，容量不足时拒绝新工作。回执被回收后，旧计划不可重放，必须重新规划。客户端应为新动作使用新请求标识；即使旧标识已可重新使用，新计划与旧计划也不是同一次执行。[账本预算与维护](../src/code_context/execution_store.py)

这套契约是选择 CoLink 协调器的核心理由。将 Codex 完整执行服务放在其下仍有可能，但网页权限、重试、恢复、写回和额度管理不会因此自动消失。

## 原文件、进程与原生隔离的实际边界

任务输入是从已授权来源派生的有限期工作集，不恢复长期完整项目镜像。进程只能修改任务盘与批准缓存；原文件仍由 WriteCoordinator 校验哈希、路径范围和恢复记录后修改。来源被用户同时修改或生成结果超出原授权范围时停止写回。当前 `move_path` 记录原始来源与当前位置，覆盖普通文件、目录及静态资源，并拒绝链接、凭据、`.git`、嵌套项目、跨文件系统和目标覆盖。本地 Git 使用任务明确选择的文件与隔离索引，检查 HEAD/索引/来源身份，保留用户暂存；同一文件的任务前未提交修改不能被顺便混入提交。Git 首版只做本地提交。[写入与移动协调器](../src/code_context/write_coordinator.py)、[受控 Git](../src/code_context/execution_git.py)

Codex 最直接的借鉴包括独立进程组、取消后确认退出、读取等待与执行超时分开，以及保留开头/结尾和明确省略量的缓冲。CoLink 没有直接将进程组当作完整后代边界：最初原型的双重 fork、`setsid` 和关闭描述符用例曾逃离进程组监督，后来增加了独立 launchd 资源组、进程出生身份和基于 audit token 的信号投递。这个失败发生于 CoLink 原型，没有运行 Codex 完整后端复现，不能据此宣称 Codex 存在相同漏洞。[Codex 进程组](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/utils/pty/src/process_group.rs)、[终止与超时](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/core/src/unified_exec/oneshot.rs)、[输出缓冲](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/core/src/unified_exec/head_tail_buffer.rs)、[当前原生监督](../src/code_context/execution_scope.py)

文件/网络层固定 SRT 0.0.78 的库接口，使用 Seatbelt 与代理过滤，再收紧到项目、任务、工具链及注册端点。CoLink 吸收了 Codex 的受保护目录祖先改名防护、主控通信目录保护和只读描述符变更防护，显式拒绝 `fcntl 80/110`；不继承用户 Shell 配置、主控凭据或未批准描述符。SRT 自身仍是需要维护的依赖，不能把其默认 CLI 策略当成本项目最终策略。[固定 SRT 实现说明](https://github.com/anthropics/sandbox-runtime/blob/6f0ce155ccb136bda33a8a72201fe7f54fe47d9b/README.md)、[Codex Seatbelt](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/sandboxing/src/seatbelt.rs)、[通信目录保护](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/sandboxing/src/seatbelt_daemon.rs)、[CoLink 策略适配](../src/code_context/resources/sandbox/helper.mjs)

保护判断以原始文件未变、违规网络未到达、资源组无成员和盘身份/退休证明等独立结果为依据。Codex 的拒绝分类也有退出码与输出关键字推断；因此“退出为零、没有违规日志”不是安全验收标准。[Codex 拒绝分类](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/sandboxing/src/denial.rs)

| 本机证据 | 证明范围与限制 |
| --- | --- |
| [文件与容量用例](../docs/TERMINAL_VALIDATION_0.5.0b4.md) | 保护来源/主控、链接及只读描述符等用例，工作盘填满后边界仍成立。填满试验使用 128 MiB 人工盘，不把它写成生产 2 GiB 全盘填满验收 |
| [网络与依赖用例](../docs/TERMINAL_VALIDATION_0.5.0b4.md) | 批准包源、依赖生命周期、Java 代理适配与绕过负例；仅限该报告覆盖的端点和工具 |
| [可信 relay 攻击用例](../docs/TERMINAL_VALIDATION_0.5.0b4.md) | 同 UID 外来资源组、叶子/祖先链接替换不能获得转发；未将探测数据交给错误服务 |
| [正式异常退出恢复](../docs/TERMINAL_VALIDATION_0.5.0b4.md) | 控制器实际 SIGKILL 后，原生证明、任务盘退休、旧计划拒绝和新任务成功；不自动恢复旧进程或继续旧生成写回 |
| [最新原生生命周期 33 项](../docs/TERMINAL_VALIDATION_0.5.0b4.md) | 正式原生监督/APFS 组件下，完成任务、最终输出确认、过期回收与活动引用保护达到预期；取消后连续 45 轮 status/output 查询没有关闭监督通道异常。TTL 使用测试时钟推进 86,401 秒，不是实际等待 24 小时 |

上述原生监督目前只对 **当前机器的 macOS / Darwin major 27** 放行。其他 Darwin 版本和非 macOS 保持终端关闭，保留只读能力；不能降级成 PPID 扫描继续授权。CPU、RSS 和进程数量是采样监督与停止阈值，快速退出的短进程可能漏计，回执仍标 `isolation_complete=false`。APFS 硬磁盘容量与软资源阈值必须分开描述，整个首版不能宣称虚拟机级隔离。[平台与身份检查](../src/code_context/execution_scope.py)、[进程监督阈值](../src/code_context/execution_process.py)、[重启磁盘恢复](../src/code_context/execution_disk_recovery.py)

## 服务、开发工具与数据库的兼容性

项目进程不直接绑定 TCP 端口。可信 CoLink 主控只在注册的 `127.0.0.1` 端口监听，转发到固定工作盘 Unix socket，并校验对端属于当前作业的原生资源组。默认开发端口为 `43117–43121`；新数据库建议端口为 PostgreSQL/pgvector `43122`、MySQL `43123`、Qdrant `43124`，只是新配置建议，不会改动用户已有服务，也不保证这些端口从不被占用。[端口默认值](../src/code_context/execution_defaults.py)、[可信 relay](../src/code_context/execution_relay.py)

端口诊断可显示当前用户监听端口、进程名称、PID 和归属，不暴露命令行或凭据。停止旧服务需要先确认 UID、项目目录、端口和进程出生身份，再经本机审核批准该具体进程；强制停止须另有明确授权。系统进程、其他项目、身份或工作目录不确定的进程不进入释放范围。[端口协调器](../src/code_context/execution_ports.py)

| 工具链 | 已实现/原生证据 | 对实际项目的要求 |
| --- | --- | --- |
| Python | 私有 venv 创建、真实 `pip` 安装、SSL/依赖导入与重启后复用；Python Unix HTTP 服务经 relay 返回正常响应 | 使用批准包源与缓存解释器；服务需按 `COLINK_SERVICE_SOCKET` 监听 AF_UNIX，不能假定任意现成 TCP 启动命令透明可用 |
| npm/Node | 真实安装与 `node_modules` 恢复，安全相对链接校验；Node HTTP 和 Vite 原生用例通过 | Node 的监听适配器覆盖已测试方式；复杂原生插件、其他框架和监听选项仍须项目级验证 |
| Maven/Java | 真实下载与编译、相同输入的离线暖构建；Java 21 Unix HTTP 与开发 Shell 的安装/报错/取消重启用例通过 | `target` 按源码摘要绑定，不能跨源码变化误复用；Java 服务需支持 Unix socket，**尚未证明通用 Spring/Tomcat TCP 服务可以透明运行** |

工具兼容证据见 [Python 与 Node HTTP](../docs/TERMINAL_VALIDATION_0.5.0b4.md)、[Vite](../docs/TERMINAL_VALIDATION_0.5.0b4.md)、[Java 21 与开发 Shell](../docs/TERMINAL_VALIDATION_0.5.0b4.md)和[正式协调器闭环](../docs/TERMINAL_VALIDATION_0.5.0b4.md)。有限命令兼容并不等于所有服务框架兼容。需要 Web 前后端同时运行的项目适合先从已验收的 Node/Python 方式接入；必须使用原样 Spring TCP 的项目，当前首版有实际功能缺口，应先解决安全适配后再决定能否采用。

`execution_environment` 返回实际存在的工具、版本及私有 Python 环境创建方式；缺少工具时给出设置提示，不自动修改全局环境。环境探测使用受限输出和干净环境；检查 Python 包元数据不会导入项目第三方代码。数据库状态进一步区分工具存在、连接配置、带密码身份验证、扩展验证与业务迁移/CRUD。[环境模块](../src/code_context/execution_environment.py)、[数据库状态](../src/code_context/execution_database.py)

本机配置的凭据保存在 Keychain 引用下，执行时经内部有界 stdin 负载传入；不将密码持久化进计划、作业元数据或命令行，并对输出进行敏感值遮蔽。数据库服务器端的实际角色权限仍是边界，不能由一份客户端配置证明。连接证明绑定 profile、作业与时间，超过 24 小时或配置改变后失效；普通连接成功不自动将业务迁移或 CRUD 标为已验证。

| 数据库 | 当前真实结论 | 尚未证明的范围 |
| --- | --- | --- |
| PostgreSQL | 独立 PostgreSQL 18.6 fixture 的 17/17 用例达到预期，包括 CRUD/重启持久化，以及错误密码、trust、管理员与危险继承角色等应被拒绝的负例 | 用户现有业务数据库与其他真实业务项目；指定人工项目的实际网页 CRUD/重启已完成 |
| pgvector | PostgreSQL 18.6 + pgvector 0.8.7 的 19/19 用例达到预期，增加向量插入、近邻查询、HNSW 创建与重启持久化 | 向量是人工三维值，没有真实 Embedding 模型；不能推出 RAG 检索质量或业务性能 |
| MySQL | 独立 MySQL 9.7.1 实例正式 13/13 用例达到预期：`partial_revokes=0/1` 精确库匹配、首次 SHA2 认证、CRUD、跨库/高权限/角色拒绝与两轮重启持久化；连接参数绑定正式 profile | 用户现有业务数据库、完整网页 MySQL 流程、TLS 身份和向量业务；已有 `3306` 实例未连接、修改或重启 |
| Qdrant | 目前仅保存连接配置 | 项目级 API 权限未验证，不向项目进程交付密钥；尚未支持该路径的实际执行 |

数据库原始证据：[PostgreSQL 17 项](../docs/TERMINAL_VALIDATION_0.5.0b4.md)、[pgvector 19 项](../docs/TERMINAL_VALIDATION_0.5.0b4.md)、[MySQL 正式 13 项](../docs/TERMINAL_VALIDATION_0.5.0b4.md)。这里的“通过”包含负例被正确拒绝，不是 17/19/13 次均允许连接。人工 fixture 的迁移与 CRUD 不会自动把用户项目的 `migrations` / `crud` 状态改成已验证；首版不会自动安装数据库、创建管理员、升级扩展或重启用户数据库。

MySQL 修复前曾错误拒绝合法转义库授权，并在默认 `partial_revokes=0` 下错误接受未转义下划线通配授权；正式人工命令确实读取了另一人工库的 sentinel。该原始失败保留在 [MySQL 修复前基线](../docs/TERMINAL_VALIDATION_0.5.0b4.md)，不能算作安全通过。当前修复区分通配模式与字面模式，正确处理库名转义，支持首次 SHA2 认证，并约束项目连接主机、端口、角色和库；新版正式原生复测证明这些预期允许/拒绝及服务端跨库拒绝成立。上述权限修复只证明列出的人工 MySQL 9.7.1 范围，没有扩大成对用户数据库或任意版本的保证。[当前数据库探测](../src/code_context/resources/sandbox/database-probe.mjs)

## 容量与单后端性能实测

250 MiB 是用户允许的**新增安装占用上限**，不等于缓存、运行内存或任务盘额度。最终 b4/build 10 常规文件分配块总量为 156622848 B（约 149.37 MiB），原 b3 为 140054528 B（约 133.57 MiB），新增 16568320 B（约 **15.80 MiB**），满足上限。采用相同 `st_blocks * 512` 测量，不把它当作 APFS 卷实际唯一物理块变化；复用的本机工具及运行缓存不计入安装包。最终自包含包、实际复制和启动后的严格签名均通过。[安装测量](../docs/TERMINAL_VALIDATION_0.5.0b4.md)

运行期默认约束如下，来自当前实现，而非性能推测：

| 项目 | 当前默认值与回收边界 |
| --- | --- |
| 并发与期限 | 2 个开发服务 + 1 个有限任务；有限执行 5 分钟，服务闲置 20 分钟停止；同一服务端 `development_task_id` 5 轮或 20 分钟，跨新 ID 的同一自然语言目标不能自动合并计数 |
| 任务工作区 | 每作业 2 GiB APFS 容量边界；原生收束确认后退休，未知身份或未确认清理保留并阻止扩张 |
| 缓存 | 每项目 2 GiB、全局 4 GiB；仅淘汰没有活动引用的旧条目，超限明确拒绝 |
| 输出 | 每作业 8 MiB、全局 64 MiB；最终确认后回收，未读完成输出最多保留 10 分钟 |
| 失败摘要 | 每项 64 KiB，最多 16 项，保留 1 小时；计入输出预算，避免另起无界副本 |
| 幂等账本 | 回执 24 小时，总物理预算 32 MiB；预留 SQLite 回滚峰值，回收同时要求清理与引用证明 |
| 辅助控制记录 | helper 准备前检查总文件数 256 与 8 MiB 边界；原生资源组控制目录有数量/单项大小约束。清理身份不明时保留并拒绝继续增长 |
| CPU/内存/进程 | 采样 RSS 2 GiB、进程 128、30 秒窗口平均 CPU 4 核的停止阈值；均为软监督 |

缓存使用相同键复用 venv、下载缓存和安全验证后的 `node_modules`；Maven `target` 另绑定输入摘要。活动引用不能因容量不足被删除，反复构建不新建整份无限缓存。[缓存实现](../src/code_context/execution_cache.py)、[协调器预算](../src/code_context/execution_coordinator.py)、[输出与资源监督](../src/code_context/execution_process.py)

以下数字来自 [正式协调器性能证据](../docs/TERMINAL_VALIDATION_0.5.0b4.md)的一次本机人工项目流程，正式协调器、NativeSandbox、资源组、relay 和 APFS 缓存真实运行。它不是网页计时、最终安装包计时或真实业务数据库测试。首次输出以 20 毫秒观察循环记录，RSS 约每 200 毫秒采样。

| 作业 | 启动接口响应（秒） | 首次输出（秒） | 总生命周期（秒） |
| --- | ---: | ---: | ---: |
| Python venv 创建 | 2.008 | 无输出 | 12.872 |
| Python pip 安装 | 2.005 | 3.393 | 5.116 |
| Python 使用环境 | 2.006 | 2.360 | 3.435 |
| npm 安装 | 2.010 | 3.863 | 5.362 |
| npm 生成 | 2.011 | 2.295 | 3.515 |
| Python 后端服务 | 2.008 | 2.352 | 8.628 |
| Node 前端服务 | 2.012 | 2.543 | 4.434 |
| Maven 首次构建 | 2.009 | 2.705 | 18.631 |
| Maven 暖构建 1 | 2.012 | 2.572 | 7.265 |
| Maven 暖构建 2 | 2.005 | 2.258 | 6.976 |
| 协调器重启后 Python | 2.003 | 2.917 | 4.170 |

计划接口在该流程约 0.686–0.954 秒。启动响应接近 2 秒反映接口的初始等待窗口，不是纯进程创建延迟；有限任务总时间还包含盘/输入/代理准备和清理。服务生命周期包含实际 HTTP 请求与取消，不能拿 8.628/4.434 秒当服务启动耗时。没有重复采样分布，不能发布 P50/P95 或跨平台保证。

Maven 的“冷”指新项目缓存中的首次构建，机器与 JDK/Maven 已存在，系统文件缓存不保证冷；两次“暖”使用同一输入摘要与离线模式。18.631 秒降至 7.265/6.976 秒说明这次工作负载的缓存有收益，但日志仍重新编译，没有 `Nothing-to-compile`，不能称完整增量编译复用。该结果支持保留缓存机制，不能据此归因于 Python/Rust，也不能排名 Codex。

控制器空闲 RSS **41.1 MiB**、采样峰值 **46.6 MiB**；任务进程同时采样峰值 **349.7 MiB**。数字不包含桌面 UI/渲染器，采样也可能错过短时峰值。流程结束保留 3 个缓存键、0 个活动引用：逻辑文件字节 **28,675,397**，项目 APFS 占用 **35,274,752**，稀疏镜像物理分配 **55,644,160**；日志/摘要 **6,352** 字节，SQLite **413,696** 字节。三种缓存占用口径不同，不能只用逻辑字节代表实际磁盘成本。

异常退出复测属于后续独立版本：运行中的正式任务在控制器 SIGKILL 后约 **435.85 毫秒**观察到旧原生资源组无成员，新协调器构造约 **460.11 毫秒**；未写回生成器的新协调器构造约 **413.07 毫秒**。正式恢复确认旧盘已退休，旧计划不可重放，新任务可执行；生成器写回明确为 `unavailable/runtime_restarted`。早期运行中用例只有旁路观察无成员、持久清理证明仍不足，产品保守关闭；早期 fixture 内部查询 `KeyError` 也有保留记录。最终通过只引用新版完整报告，不把这些失败算作通过，也不将不同版本拼成一次验收。[运行中完整复测](../docs/TERMINAL_VALIDATION_0.5.0b4.md)、[生成器完整复测](../docs/TERMINAL_VALIDATION_0.5.0b4.md)

## 维护、许可与后续选择条件

完整 `exec-server` 依赖配置、文件系统、网络代理、沙盒、协议、HTTP/WebSocket、安全与其他模块；它不只是一个进程启动库。`codex-utils-pty` 的依赖面更小，没有其他 Codex crate 依赖，未来可以考虑抽取或通过窄接口复用，但仍需处理 Rust 构建、工作区依赖解析、分发与进程/协议边界。当前保留一套后端，先避免两套会话、取消、输出与安全行为长期分歧。[PTY 依赖清单](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/utils/pty/Cargo.toml)、[完整执行服务依赖](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/exec-server/Cargo.toml)

自有协调器节省的是与既有授权/写回协议重接的工作，并不意味着维护天然更轻。SRT 固定版本、Seatbelt 策略、launchd/内核身份接口、Unix relay、包源代理和工具链变化均需回归；其他 Darwin 版本关闭也意味着系统升级可能暂时失去终端能力。若以后项目主要依赖通用 Spring TCP、复杂交互 CLI 或多平台终端，应重新评估兼容成本。保留边界与明确不可用状态，比把未适配场景包装成已支持更有决策价值。

窄 Rust 模块仅在出现明确收益时考虑：需要可靠 PTY/输入/尺寸能力，或同一批准工作负载的测量显示进程/输出层占据主要开销，或自有平台适配的维护成本已超过模块接入成本。对照必须保持输入、权限、隔离、缓存冷热条件和输出容量相同，同时测启动/首输出、取消收束、内存、长期占用与最终安装体积；不能通过降低安全策略或省略恢复功能换取表面速度。即使替换进程层，网页幂等、项目授权、原文件恢复和数据库证明仍由 CoLink 协调器负责。

已借鉴的 Codex Seatbelt 防护在源码中注明来源与改动，项目保留了 [Codex LICENSE](../src/code_context/resources/sandbox/licenses/codex/LICENSE)和[NOTICE](../src/code_context/resources/sandbox/licenses/codex/NOTICE)。分发改编部分时保留适用 Apache-2.0 许可、归属、NOTICE 与修改声明；CoLink 自身许可不能替代这些要求。NOTICE 内还有第三方归属，打包时需按实际包含内容核验，不能只保留文件名称。[固定 Codex LICENSE](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/LICENSE)、[固定 Codex NOTICE](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/NOTICE)

对当前项目，已验证的网页开发流程支持继续采用这套组合架构，最终安装预算已达标；没有同负载 Codex 对照，不能宣称性能胜出。当前明确缺口为通用 Spring TCP、PTY、Qdrant 实际执行，以及用户新提出的项目配置自动解析、环境快速导入和“只读/开发者”统一模式。实际数据库首版仍使用本机 profile 与 Keychain 配置，没有把项目配置自动当作数据库目标；现有两个开关也没有自动完成新模式。下一轮按用户优先级先完成测试清理，再收敛这些使用流程，保留已验证的授权、隔离、冲突与恢复边界。
