# CoLink · 架构与协议 v1

2026-10-06 实施补充：下文主体保留**旧 mirror 兼容架构与协议**，不是新 live 的读取
语义。开发分支 0.5.0-alpha.1 已引入 SourceAccess / LiveQueries / ProjectRegistry / 有界
ReadContexts、单生产线程按需 LiveIndexService 和 WatchCoordinator。本机私有控制
与网页 MCP 分离，WriteCoordinator / RecoveryStore 负责默认关闭项目授权、任务起点、
先保存后提交、持久幂等、整项回退和冲突恢复；未完成操作时停止该项目正常查询发布。
live 索引只存当前结构事实，读取直接核验原文件；`get_diff` 的 previous 参数表示
保留任务起点，而非不可变旧源码快照，返回 task_origin 或 NO_TASK_BASELINE。
旧 mirror 工具的历史语义不改，旧数据库不删除。当前本地/网页证据分别见
[实施记录](IMPLEMENTATION_LOG.md)和[网页 SOP](WEB_ACCEPTANCE_SOP.md)，未完成真实网页验收。

本文描述当前已实现的镜像架构。2026-10-06 记录的下一次重构为“先解耦，再原文件
按需直读、多项目工作区与按需索引”，详见
[LIVE_WORKSPACE_REFACTOR.md](LIVE_WORKSPACE_REFACTOR.md)；尚未实施，不能把目标图或
新读取语义当作现有运行能力。既有镜像、历史状态和来源限制在迁移确认前继续有效。

```text
指定项目目录
    │ 只读取允许的 UTF-8 文本
    ▼
Scanner（过滤 / 稳定读取 / SHA-256 / 对账）
    ▼
LocalState（已确认状态 + 不可变 outbox）
    │ 本地主动发起 HTTP/HTTPS
    ▼
同步接口 /api/projects/{project_id}/sync
    ▼
MirrorStore（SQLite 原子提交 / 不可变快照 / 内容去重 / Python-Java 结构索引）
    ▼
只读 MCP：stdio 或 /mcp
    ▼
客户端按路径、文本、结构关系和固定内部 snapshot 标识按需查询
```

## 模块职责

| 文件 | 职责 |
| --- | --- |
| `cli.py` | 参数、环境变量、用户输出、进程生命周期 |
| `policy.py` | 客户端/服务端共用路径、文件与凭据过滤规则、大小限制 |
| `models.py` | 严格的协议模型、哈希与路径校验 |
| `scanner.py` | 不跟随符号链接的安全文件采集、增量刷新和对账 |
| `client.py` | 单来源锁、持久化 outbox、同步、监听、退避 |
| `local.py` | 单项目本机生产端、来源绑定、本地 outbox、监听和状态 |
| `tunnel.py` | 无凭据 profile、安全密钥加载、官方客户端与回环健康检查 |
| `desktop.py` | 原生应用的本机监督进程、来源隔离、全连接关闭和只读状态 |
| `macos/CodeConnect/` | 原生 SwiftUI/AppKit 菜单栏、系统文件夹选择和透明材质 |
| `storage.py` | 快照事务、幂等、revision 冲突、读取与搜索 |
| `intelligence_models.py` / `intelligence_python.py` / `intelligence_java.py` | 有界语法事实与两种语言解析，不执行项目代码 |
| `intelligence_roots.py` / `intelligence_resolver.py` / `intelligence_index.py` | 镜像内源码根配置/发现、保守静态绑定、哈希复用、生产端原子索引与两份状态清理 |
| `intelligence_queries.py` / `intelligence_tools.py` | 只读符号、引用、关系、层级、循环和影响查询 |
| `server.py` | MCP 工具注册、生命周期、HTTP 认证和独立同步接口 |
| `demo.py` | 保留数据的端到端演示 |

## 上传协议

本机模式沿用下面的 HTTP 同步协议。0.3.3 镜像 schema 升至 2，客户端状态仍为 1；
LocalMirror 复用批次生成、
LocalState、watchfiles 与 MirrorStore，直接在本机事务提交。提交与确认之间崩溃时，
先用同一 request_id 重放，再采集停机期间的新修改。新数据目录绑定唯一 root/project。

本机采集与 stdio 查询分开：启动前先安全对账，后台生产端监听后续变化；十五个工具只
查询已提交快照，不采集、更改或执行源目录代码。`project_scope` 拒绝其他项目；
监听不可用时工具 fail closed。不同根目录不能借用既有绑定目录的历史快照。

`connection_status` 不触发采集，也不要求监听器 ready，因此源读取失败时仍可诊断。
它只返回状态字段、时间和过滤规则，不返回源绝对路径、密钥或源码。没有本机监听器的
静态镜像服务必须标为 `mirror_only`，不能把最后一个快照称为实时同步；隧道完全离线
时任何 MCP 工具都无法回答，状态中的 `tunnel_status` 明确为不可观测。
`last_seen` 是本机处理这次状态查询的时间，不是远程独立心跳；`last_sync_at` 是最近
成功对账/同步的时间，无改动时对账成功也会更新，不能从它推导用户代码最后修改时间。

```text
指定单一目录 → LocalMirror → 本机 MirrorStore → 受限只读 stdio MCP
                                             ↑
                                     官方 tunnel-client
                                             │ 主动出站 HTTPS
                                      OpenAI 私有 Tunnel
                                             ↑
                                       获授权的 ChatGPT
```

Tunnel profile 固定官方控制面、一个 stdio 命令和回环管理端点。运行密钥单独位于
私有 `.env.local`，不进入 profile/outbox/命令行。组织与工作区授权由 OpenAI 控制面
管理，不由本机静态上传令牌代替。扩大目录、项目、组织或工作区必须重新确认。

`POST /api/projects/{project_id}/sync`，使用同步令牌。示意消息：

```json
{
  "protocol_version": 1,
  "request_id": "unique_request_id",
  "base_revision": 1,
  "mode": "delta",
  "changes": [
    {"op": "upsert", "path": "src/main.py", "content": "源码文本", "sha256": "64位SHA256"},
    {"op": "delete", "path": "old.py"}
  ]
}
```

哈希示例是占位符，不是可直接提交的消息。初次上传 `mode=full`、base_revision=0。
已存在项目只接收 delta，不接受整库覆盖。每个请求路径唯一；服务端再次验证源码哈希、
大小、文件策略与 JSON。UTF-8/BOM、重复 JSON 键、非法路径和额外字段不得绕过校验。

提交在 `BEGIN IMMEDIATE` 中完成：校验幂等记录 → 校验 base_revision →
计算最终文件清单 → 校验配额 → 写入快照与幂等记录 → 建立同状态结构索引 →
更新最新指针 → 清理超出两份窗口的派生历史 → 提交。
异常自动回滚。保留窗口内相同 request_id 与相同消息返回原 revision；同 ID 不同消息被拒绝。
版本不一致返回 409，客户端保留批次并停止，避免自动覆盖另一个来源。

本地队列只允许一个批次。成功确认后，将“批次中实际发送的哈希”写为已确认状态，
不是把此时磁盘上的新内容误记成已上传。再对当前磁盘生成下一批。持久化状态与
确认/移除 outbox 在一个事务内更新，重新运行可以继续处理。

## 快照语义

revision 在每个项目内单调递增，仅保留在内部同步协议、CLI 和 HTTP 维护接口；
MCP 参数与结果不再暴露这个编号。`files` 为当前和前一份状态保存完整清单，
`blobs` 按 SHA-256 去重。保留期间的快照不可变；每次提交在同一事务中删除更早清单、
快照、相应幂等记录和全库无引用内容，多项目共用内容只在完全无引用后删除。

`snapshot` 是项目绑定的随机读取标识，可用 `current`/`previous` 解析或从清单取得。
编号不会重置，以免未确认批次冲突；旧保留窗口之外的请求因 base_revision 过期被拒绝。
已保留两份状态内的相同 request_id/payload 仍返回原确认。单来源 outbox 必须先确认
当前批次再生成下一批，因此提交后丢失确认、断网和重启仍可恢复。

读取通过显式 SQLite 读事务固定一次查询；当前/前次比较在一个读事务中同时解析和
加载两份内容，写入者并发清理不产生混合结果。跨工具调用用同一内部标识；过期明确
拒绝并要求重新分析，不静默切到当前。目标仍存在但比较基线已移除时单独说明基线不可用。

首次状态没有历史基线时返回 `NO_PREVIOUS_SNAPSHOT`，不会隐式与空目录比较。
默认 diff 只计算摘要和分页文件描述，只有显式 `detail="patch"` 才返回补丁源码。
概要分页和补丁调用固定相同目标标识；基线过期报 `COMPARISON_BASELINE_UNAVAILABLE`。
`baseline="empty"` 是调用者主动选择的初始目录比较，并不创建或保存新历史。

schema 1 升级在事务中保留两份并补齐随机标识，持久化 compaction_pending 后压缩。
中断后重启继续回收，保留已生成标识。新数据库启用增量回收，提交时回收少量空页，
其余复用；WAL 大小策略不能替代读者及时结束事务，不宣称整个目录有固定物理大小上限。
MCP 工具读取本身不进行清理，保留只读注解；源目录始终只读。

采集并不是磁盘原子快照：单文件稳定读取和监听后的补偿扫描，使远端向指定目录的
当前状态收敛。远端读取可以固定保留期间的代码状态；不能据此宣称模型已经拥有整库上下文，或者
把某个静态快照时间当成客户端仍在线的证明。

## 维护约束

协议版本、数据库 user_version 与包版本分别维护。同步协议/客户端 schema 为 1，
镜像基础 schema 为 2，当前开发包版本为 0.4.2；新增 `ci_*` 派生表，不改变上传协议
或原始表格式，不自动升级未知数据库。派生表按解析版本启动回填，外键关联对应快照。
增加工具优先复用 MirrorStore 的查询能力。加入索引时以 project_id + revision +
content_hash 标记派生状态。变更测试通过后记录 CHANGELOG；自动清理只针对用户已确认的
两份快照窗口，配置、密钥、数据库本体、依赖、测试和构建产物不属于这个清理范围。
结构索引的功能、预算、增量边界及静态解析限制见 [CODE_INTELLIGENCE.md](CODE_INTELLIGENCE.md)。

当前上传令牌与读取令牌是单用户、整服务范围，不能承诺租户或项目级权限隔离。
公网 HTTPS ChatGPT 接入需要另行完成 OAuth，保持同步端点只接受上传设备的凭据。
官方私有 Tunnel 路径不暴露 HTTP 同步接口，也不增加多租户能力。

## 原生应用生命周期

CoLink 原生应用不是新增的 MCP 工具。用户在本机界面操作后，原生应用通过受控的
`desktop-run` / `desktop-status` 本机 CLI 管理既有隧道，不允许网页启动命令、
选择目录或操作本机文件。原生 UI 从不解析密钥；沿用原有安全 launcher。

监督进程独占 `.code-context/desktop/connection.lock`，创建自己的子进程组；关闭
请求、控制管道 EOF、SIGINT/SIGTERM 或客户端异常退出均进入整组清理。只停止由
自己创建的进程，不从状态文件读取 PID 后随意杀进程。没有重启循环和持久化运行意图。

重开应用只检查状态，不启动连接。选择文件夹只保存本机选择；非样例目录启动前
由用户确认源码共享。每个根路径映射到独立 project/data/profile，保持旧快照隔离；
重选原样例复用已存在样例历史。历史目录被移动后仍可查看锁/旧状态，不绕过启动时的
nofollow 来源检查。只读状态不生成 profile，不采集源码，也不访问 OpenAI API。
