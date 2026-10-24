# M1：定向结构查询与联合入口

M1 初次交付于 CoLink 0.5.0b6 / build 26；当前接口维护版为 0.5.0b7 / build 33。
本文件描述接口与验证方法；实际安装、网页结果和性能数值以对应验收收据为准。
M2 Python 数据流及其静态分析边界见 [M2 说明](CODE_INTELLIGENCE_M2.md)。

## 查询实现

- Live 调用/类型图按 BFS 前沿定向查源、目标符号索引，按层批量加载符号。每个
  前沿节点有独立读取上限，排序和调用点身份保持既有语义；自边只取一次。
- 文件依赖只解码当前前沿涉及的完整文件对，保留聚合计数、前五条证据、类型专用
  标记及同文件导入。全项目架构和 SCC 继续使用完整关系图。
- 历史镜像的同键排序受旧查询计划影响，保留原邻接 SQL，符号可批量读取。
- 不新增索引表、持久游标、后台服务或数据流缓存。反向路径索引先在独立生成库
  评估端到端 P95、查询计划和体积门槛；没有满足全部门槛的候选不进入产品。

## Live 只读工具 analyze_code

| 模式 | 选择器 | 内容 |
| --- | --- | --- |
| file_context | path | 分页符号、归属、可选调用、文件依赖与实际正文 |
| symbol_context | symbol_id 或 path + qualname | 精确符号、祖先/后代归属及可选关系与实际正文 |
| callers / callees | 精确符号 | 入向/出向静态调用 |
| call_graph | 精确符号 | outgoing / incoming / both，多层调用图 |
| call_path | 精确符号 + target | 有向最短静态调用路径，最多十条等长路径 |
| class_relations | 精确类型符号 | 继承、实现、归属、类型引用及实例化 |
| file_dependencies | path | 指定方向的文件依赖 |
| diff | 可省略或仅 path | 写入任务起点与当前文件的 summary / patch 比较 |

示例参数：

```json
{
  "project_id": "从 list_projects 返回的项目",
  "mode": "symbol_context",
  "selector": {"path": "service.py", "qualname": "Service.run"},
  "include": ["ownership", "calls", "source"]
}
```

重复定义返回 analysis_status=ambiguous 和分页候选，客户端选 symbol_id 后继续。
禁止额外字段与类型自动转换，不凭名字猜测定义。build 33 使用 `read_code` 定位与
读取源码；`analyze_code` 用于必要的结构、数据传播和任务比较。八个被替代的旧
Live 名字已取消注册，缓存旧 schema 的直接调用也会失败；CLI 兼容发现开关已移除。
历史镜像十五工具保持原样。build 32 当时仅隐藏四个旧分析入口，仍可调用；当前
删除是 build 33 的新行为，不能把历史验收当成已完成删除。
file_dependencies 保留 include_type_only 选项；diff 的历史基准和分页语义复用原入口，
不经过结构索引。引用、反向文件依赖影响和全项目视图保留各自语义。Live 只读工具
为 9 个、附加写入为 19 个、完整工作区为 40 个，发现集合与实际注册一致，见
[读取与分析入口](CODE_ANALYSIS_ENTRYPOINT.md)。

所有非 diff 模式可在 `include` 中追加 `source`；上下文模式还支持 `ownership`、
`calls`、`dependencies`。正文放在 `source` 字段中，只读取当前 selector 的文件或
准确符号，不自动读取 target 或全图节点。正文和关系使用同一有效上下文，共享
max_chars 与剩余 max_ms；附正文时 max_chars 至少 3000。歧义返回候选，不任取正文；
正文预算不足时按 source 的部分状态与续页信息定向读取。已知文件仅需正文时用
`read_code`，不必先建立结构索引。

默认深度 1、节点 50、边 100、输出 12,000 字符、计算预算 3,000 毫秒；上限为深度
5、节点 200、边 500、输出 50,000 字符、计算预算 10,000 毫秒。节点、边在组合
视图间按类型去重并共享额度。limit 默认 20、最大 100，offset 最大 1,000,000。
结果裁剪完整事实，保留端点一致性、截断原因和继续查询信息。分页不设常驻游标。

## 一致性与能力边界

一次请求共用授权、有效 Live 上下文、索引等待和源码前后校验。单条 SELECT 在
现有锁内读取并脱离连接，Python 遍历在锁外运行；SQLite 进度回调中断超预算计算，
结束仍重新验证发布身份及源码。索引等待和安全校验不计入计算预算，不承诺请求
整体在三秒内完成。部分索引不能声称完整分析。

工具只提供已保存源码的静态证据，不执行项目。未解析目标、缺失路径或截断图
不能证明实际运行不可达。源码读取、写入、终端与数据库权限流程保持既有约定，
安装不自动开启开发档；正常业务仍续延 300 秒滑动空闲窗口。

## 可复核证据

本轮收据在 `.artifacts/intelligence-upgrade-20261010/`，评估原型在
`.artifacts/intelligence-evaluation-nr7qn5v5/`。`scripts/benchmark_intelligence_upgrade.py`
生成独立源码与真实 Live 压缩库，采用至少 50 次热查询，分别记录组件、完整 Live
链路 P50/P95、SQL、解码数量、响应、SQLite 配置和体积。冷启动、单次更新与重启
是功能观察，不以一个样本声称 P95。测试不构成模型 Token、电池能耗或网页延迟
收益证明。保留本轮产物，不清空用户持久数据或移动恢复锚点。
