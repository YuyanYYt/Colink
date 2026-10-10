# 统一代码分析入口 · build 32

当前 Live 默认完整工具集合由 46 个减为 42 个，只收拢四个已被覆盖的入口。没有
新增 MCP 工具或更改写入、终端、数据库、Git、端口权限流程；历史镜像保持 15 个
原有只读工具及原 snapshot/previous 语义。

| 默认隐藏的兼容工具 | analyze_code 模式 | 参数对应 |
| --- | --- | --- |
| get_call_graph | call_graph | symbol_id → selector；depth → max_depth；保留 direction |
| get_class_graph | class_relations | symbol_id → selector；depth → max_depth；保留 direction |
| get_file_dependencies | file_dependencies | path → selector.path；depth → max_depth；保留 include_type_only |
| get_diff | diff | 可选 path → selector.path；保留 baseline/detail/offset/limit/max_chars |

隐藏只改变 tools/list；兼容名字仍在服务端注册，已缓存旧定义的客户端仍可调用。
源码用户可通过 live/workspace 的 --legacy-analysis-tools 或 build_mcp 的
expose_legacy_analysis_tools=True 显式恢复四个 schema。默认本机应用不恢复这些
重复入口；网页需要刷新连接工具，旧会话的模型缓存不能由本机安装自动消除。

保留 symbol_search、read_symbol、find_references：分别返回定义候选、实际源码、
含导入/类型的静态引用，不能用 symbol_context/callers 代替。get_impact_analysis
仍表示保守的反向文件依赖影响，data_impact 表示一个值的传播，两者语义不同。
全项目架构、外部依赖、源码文本搜索继续使用原工具。execution_* 和 terminal_*
两种执行路径及其权限没有合并。

## 统一查询示例

双向调用图：

```json
{"project_id":"已授权项目","mode":"call_graph","selector":{"path":"service.py","qualname":"Service.run"},"direction":"both","max_depth":3}
```

文件依赖过滤类型专用关系：

```json
{"project_id":"已授权项目","mode":"file_dependencies","selector":{"path":"service.py"},"direction":"incoming","include_type_only":false}
```

查看最近仍保留的写入任务相对于起点的修改：

```json
{"project_id":"已授权项目","mode":"diff","selector":{"path":"service.py"},"detail":"patch","limit":20}
```

diff 可省略 selector 获取分页 summary。baseline=previous 指当前仍保留的写入
任务原始文件，不能解释为任意外部修改或 Git 历史。NO_TASK_BASELINE 表示历史
不可用，analysis_status=unavailable；不是没有改动。baseline=empty 必须显式传入。
该模式复用原任务文件验证、恢复对象和 Live 前后校验，不构建/访问结构索引。
输出预算只裁剪 patch 文本，保留完整变更描述和扫描分页位置；无法容纳元数据时
要求缩小 path/limit 或提高 max_chars，不伪造分页。其它模式要求文件/符号/值选择器。

file_context/symbol_context 的 include 可在一条请求里读取多个结构部分，combined
可在一条请求里返回结构与数据流；本版不增加任意工具批处理或命令执行入口。

## AST 缓存与验证证据

build 32 实现选择性内存 AST LRU，没有方法摘要或持久数据流图。源码至少 32 KiB
且本次原生解析至少 5 ms 才准入；工作区合计最多 4 个 AST / 32 MiB 估算驻留。
源码、项目身份、索引与请求预算校验保留；每次重新计算数据流和调用上下文。
详情见 [M2 边界](CODE_INTELLIGENCE_M2.md)。五分钟滑动窗口及索引关闭一并释放缓存。

新旧图入口等价覆盖三种图、三个方向、三个深度及正常/截断预算，共 54 组；另有
类型关系过滤、diff 基准/补丁/分页、旧上下文拒绝、兼容发现及严格 schema 验证。
缓存覆盖准入/容量/LRU、源码修改、项目隔离、并发、只读 AST、在途清空、预算、
闲置/关闭释放与重启；性能用三个独立合成场景、四个模式交错各 50 个热样本。
结果不等于网页延迟、模型 Token 或电池收益。验收收据在
`.artifacts/analysis-upgrade-20261011/`，安装与网页证据分别记录。
