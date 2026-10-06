# CoLink · 直读重构与受控写入要求对照

日期：2026-10-06。源码检查点 `a1a7053` / `0.5.0b1`；开发分支
`codex/colink-controlled-write`。本文是目前证据和缺口清单，**不是最终完成声明**。
原需求不缩减：[LIVE_WORKSPACE_REFACTOR.md](LIVE_WORKSPACE_REFACTOR.md)、
[WRITE_CONTRACT.md](WRITE_CONTRACT.md)。

## 要求、实现与证据

| 要求 | 当前实现/验证 | 限制或剩余门槛 |
| --- | --- | --- |
| 不可移动 0.4.3 回退点 | 标签仍指向 `1fc98de`，应用 ZIP 与源码 bundle 两项 SHA 校验通过；旧安装/数据库保留 | 不是数据库备份或异机灾备；降级前仍须兼容核对 |
| 先解耦、保留原十五工具 | QueryBackend / MirrorQueryBackend / LiveQueries；原 stdio/HTTP 镜像兼容仍读两份历史 | 新 live context 不是不可变历史快照，不将两种 Diff 混淆 |
| 当前源码按需直读 | SourceAccess / ReadContexts；哈希、来源、忽略和参与文件变化核对；LF/CRLF 原文行号 | 不保证外部编辑者被锁住；旧上下文变化明确拒绝 |
| A～G 多项目与隔离 | ProjectRegistry/私有控制；项目 ID 路由，A～G/同名/嵌套/Maven、撤权/来源替换本地回归 | 网页只能使用已启用项目；实际网页仅样例，不冒充多企业仓现场 |
| 监听与按需关系索引 | 一条监听线程、非递归分层、剪枝/脏标记/补偿；Python/Java 事实按查询项目更新 | 关系是静态事实，动态或无法绑定标为未知，不冒充完整动态图 |
| 大文件/合理延迟/有界资源 | 4 MiB 文件、分页；5000 定义文件重复结构请求中位 0.838 秒，cold 2.866 秒；索引 64/128 MiB 等独立预算 | 本机约 3.77 MB 合成组件，不含 Runtime/隧道/模型或真实企业压力；不是 P95 |
| 原十五工具实际网页可用 | 每个工具在回退后的新样例上下文实际调用；结果/关系与两份原文件一致 | 仅两文件 Python 样例；具体结果见网页记录，不外推全部 Java/账号 |
| 默认关、本机授权 | 原生项目选择无预勾，只授权样例，待后台确认；实际网页默认关/撤权后 `WRITE_DISABLED` | 平台确认独立；实际 UI 关闭/退出/重启仍待验 |
| 先保存、精准写入/创建 | WriteCoordinator / RecoveryStore / 原生原子安装；准确原文/哈希保护，实际三轮片段/按行/目录与文件 | 无通用终端/删除工具；macOS 原生事实，其他平台相关分支仅模拟 |
| 同任务多轮、幂等/过期 | 三轮同任务累积 Diff；相同请求重试只一笔操作；旧 SHA 真实网页拒绝 | 新操作必须新请求标识，不同操作复用会正确拒绝 |
| 无镜像仍有真实 Diff | 首次触及文件保留任务起点；原 get_diff 逐轮汇总，回退后零；无基线明确不可用 | 不能还原未保存的任意外部历史；恢复起点不随每轮改动覆盖 |
| 整项回退与外来保护 | 网页整项恢复两个原文件/撤销新对象；外来目录项先冲突、零部分更改；原生按钮也真实恢复 | 多文件不是同时原子；不撤销运行副作用/数据库/网络操作 |
| 崩溃/中断与恢复 | 持久意图/身份/正文/属性、未结束发布屏障、本机受控续做、重启不继承 grant 的本地专项通过 | 注入故障/本地生命周期不等于实际断电或网页重启界面 |
| 不堆积无限副本 | 全局恢复 64/128 MiB、元 DB 16 MiB、1024 操作、最近一项/七天；300 次只保留必要正文；网页跨任务也实际退休前项 | 活跃/冲突/待恢复受保护；不足拒绝新写；不自动清理旧库/构建产物 |
| Skill 与无需手选 | 仓库/现有网页副本描述、正文和隐式策略已更新保存，名字/图标/身份不变 | 更新后的新对话自然触发/充分代码反例仍待；不能强制模型调用 |
| 自包含交付 | 通用 beta.1 DMG/ZIP 构建、签名/重定位/归档 SHA/公开内容保护及包内真实 stdio 22 工具生命周期通过 | 未装系统、未推 GitHub；最终 GUI 与正式 0.5.0 仍待，不分发私有验收 runtime |

本地关键门槛 **1813 passed（79.64 秒，1 条既有上游弃用警告）**；相关 beta packaging/
publication/原生资产 **84 passed（3.39 秒）**。测试使用新独立目录，保留所有负向材料。
全量只在关键节点执行，不把总数当成真实网页、业务质量或安全审计。

## 可检查的主要来源

- 当前真实网页与原生界面结果：[WEB_WRITE_VALIDATION.md](WEB_WRITE_VALIDATION.md)。
- 组件性能/RAM/SQLite 测量：[LIVE_READ_BENCHMARK.md](LIVE_READ_BENCHMARK.md)。
- 写前副本/任务/额度/原子与不确定状态：[WRITE_IMPLEMENTATION.md](WRITE_IMPLEMENTATION.md)。
- 只读兼容/来源边界：`test_query_backend.py`、`test_source_access.py`、`test_read_context.py`、
  `test_live.py`、`test_stdio.py`、`test_local.py`。
- 多项目/监听/索引：`test_project_registry.py`、`test_workspace.py`、`test_live_watch.py`、
  `test_live_index.py`、`test_python_source_roots.py`、`test_large_projects.py`。
- 写入/属性/容量/任务：`test_write_operations.py`、`test_write_attributes.py`、
  `test_write_tasks.py`、`test_write_directories.py`；失败/中断/整体恢复：
  `test_write_recovery.py`、`test_write_rollback.py`、`test_write_diff.py`、`test_write_milestone.py`。
- 真实本机通道/stdio/原生源码守卫：`test_local_control.py`、`test_desktop_write.py`、
  `test_workspace_write_stdio.py`、`test_macos_assets.py`；交付：`test_packaging.py`、`test_publication.py`。

## 候选包与保留材料

通用 beta.1 输出在 `.artifacts/w-beta1-delivery.z1TfWA/generic/`，不绑定维护者状态；
实际私有网页使用更早 alpha.2 独立验收包，核心未改，beta.1 只补终态文案/Skill/说明。
不能把候选包自己的 stdio 证明写成已在网页或系统安装该包。

- 包版本/内部路径/依赖随 `package-report.json` 留证；应用逻辑 **135644919 字节**，
  文件系统分配 **140017664 字节**。ZIP **54687888 字节**，DMG **69540753 字节**。
- 通用包公开内容检查 **1861 files / 0 findings**；签名、重定位/源码导入和归档 SHA 通过。
- 包内 Python 实际跑既有三轮 stdio 演示：22 工具、默认关、仅测试项目本机授权、精准
  修改/重试/创建、原 Diff、整项回退及撤权通过；没有启动通用包 GUI 或向系统目录写数据。
- 全量材料在 `.artifacts/w-beta1-key-gate.bxwChi`；已知权限负向测试故意保留不可读 temp，
  不为统计空间而修改其权限。旧阶段产物和运行恢复数据库不自动删除。

## 最后必须补的实际门槛

电脑再次锁屏；已先撤权并停止本次自有验收连接，样例已恢复。解锁后继续：

1. 修正终态的实际界面确认、从曾开启写入状态正常关闭/退出、重新打开/连接仍默认关，
   再由网页请求确认不能写入。已有本地协议不能代替这个界面结果。
2. 更新后新对话不手选技能的自然本机读取，以及充分代码片段反例；核对实际调用而非口头声称。
3. 对上述结果再决定正式 0.5.0 和最终包；仍不自动系统安装/公开发布/清理。

不得以测试已绿、包已生成或预算接近用尽替代这些待验项。没有持续后台重试或自动开启权限。
