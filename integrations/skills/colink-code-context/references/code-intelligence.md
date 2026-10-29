# 代码智能查询细节

仅在当前工具确实提供相应模式时使用。以实际 schema 和结果中的覆盖信息为准；此文件描述选择方法，不承诺任意文件都能得到结构或数据图。

## 统一源码读取

当前 Live 的 `read_code` 顶层是 `project_id`、`request`、可选 `snapshot`、`max_chars` 和 `max_ms`；模式字段放在 request 内，不传旧接口的平铺字段。各模式严格禁止未知字段与类型转换。

- `file`：准确 `path`，可选 `start_line`、`end_line`、`char_offset`；已知文件可直接读，不要求先建立索引或调用分析。
- `symbol`：准确 `symbol_id` 或 `path` + `qualname`，两种选择器不混用。重复定义返回 candidates；先消歧，再读正文。符号页使用 line_offset/max_lines/char_offset，候选页使用 candidate_offset/candidate_limit。
- `search`：`kind=text` 或 `kind=symbol`、`query`；按需加 path_prefix、limit 和 context_lines。定义搜索还支持 exact、symbol_kind、language。从 next_cursor 续页，复用同一 snapshot、查询与过滤条件，不推测 offset。
- `batch`：items 最多五个显式文件范围，返回 items 与 next_cursor；续读保持原 items，使用返回的 item_index/start_line/char_offset 作为 request.cursor。

默认总响应 20,000 字符、时间预算 3000 毫秒；max_chars 上限 50,000，max_ms 上限 10,000。所有模式和批量项目共享总响应预算，搜索还限制扫描字节；context_lines 也计入总量。附带的搜索 context 包含真实文件 sha256/size，原文覆盖必要范围且未截断时可用于写入校验。文件页按 next_start_line/next_char_offset，符号页按返回的定义相对位置续读。不要把截断正文当成完整定义。

## 联合结构入口

2026-10-29 开发源码的日常 `analyze_code` 可省略 mode：文件选择器取文件上下文，
精确符号取符号上下文，一个明确值取来源与影响，两个值取值路径，两个精确符号
取调用路径。缺少值时不猜参数、不自动启动值流。旧显式 mode 仍作兼容策略严格
校验，但不出现在默认发现 schema 中。先核对实际工具版本；旧安装版不能套用
源码新接口。Codex 会话遵守技能入口的使用边界，使用本机工具而非连接工具。

`analyze_code` 在一个经校验的读取上下文内组合需要的结构证据：

- `file_context`：`selector` 只传准确相对 `path`，按需分页文件中的定义。
- 符号模式：`selector` 使用准确 `symbol_id`，或 `path` + `qualname`。两种形式不混用；名称搜索先消歧，重复定义返回候选时选择实际目标。
- `symbol_context` 取得定义与归属；支持时用 `include` 选择 `ownership`、`calls`、`dependencies`。所有非 diff 模式还可追加 `source`，只附带当前 selector 的准确文件或定义正文，不自动读取 target 或全图节点。不因联合入口可用而请求全部内容。
- `callers` / `callees` 查看静态入向 / 出向调用；`call_path` 还需准确目标符号。路径有方向，选来源和目标时与用户问题对应。
- `class_relations` 定位准确类型；`file_dependencies` 定位文件，再按问题选方向。

同一轮的 `snapshot` 与其他源码查询一致。普通符号页按 `next_offset` 继续；模糊选择先取候选再选择，不把第一个同名结果当成正确目标。关系与 `source` 共享 max_chars 和剩余 max_ms，附正文时 max_chars 至少 3000；source 部分或超时则定向续读，不声称正文与关系完整。计算预算不保证索引等待与必要安全校验的整条墙钟时间。

build 33 的 Live 已取消八个旧读取/图入口的注册，缓存旧名直接调用也失败；刷新连接工具。历史 Mirror 保留原 `symbol_search`、`read_symbol`、`search_code`、`read_file`、调用 / 类型 / 文件图及 Diff 工具，按旧快照语义使用，不声称具备 Live 联合入口。结构工具没有覆盖的部分用文本搜索和实际源码说明。`find_references` 不能用 callers 代替，反向文件影响不能用 data_impact 代替。

## 值与数据传播

数据查询先定位方法和具体值。仅一个变量名不足以确定词法作用域、赋值位置和调用上下文。

- `data_origin`：向前追溯选定值的来源。
- `data_impact`：向后追踪选定值的可能影响。
- `data_path`：同时选择源值和目标值，寻找静态传播路径。
- `combined`：在同一请求中查看结构与数据传播，区分不同节点及边类型。

当前 schema 提供的值选择方式为：

```json
{"kind": "parameter", "name": "value"}
```

```json
{"kind": "return", "line": 7}
```

```json
{"kind": "occurrence", "line": 6, "column": 14}
```

这些是选择器形状示例；实际名称与位置必须来自当前源码。将 `value` 放进方法选择器：`symbol_id` + `value`，或 `path` + `qualname` + `value`。行号从一开始；列号按工具要求使用从一开始的 UTF-8 字节位置，不能把字符索引直接当成字节位置。

多处返回、重复定义或多个值候选时，使用具体定义及源码位置缩小范围，不任选一个。返回的 `df_*` 值节点和调用上下文只在该请求中有效；后续查询重新用方法身份及源码位置选择，不跨请求复用临时节点 ID。`data_path` 的目标也必须包含准确方法和值选择。

`include` 用于当前 schema 支持的组合或结构模式；不要给单独的数据模式塞入无关字段。需要分析方法内部且工具支持时可用 `max_depth=0`；跨方法只展开与问题相关的调用距离。

## 范围、覆盖与结论

从默认的小图和浅层开始，按需要调节 `max_depth`、节点、边、字符与计算预算。组合视图共享预算，不能把各视图上限相加；超时或图裁剪时读取 `analysis_status`、`truncated_reason`、继续查询信息和覆盖说明，再缩小问题或按具体节点分开查询。

区分已解析的结构事实、可能传播和未知关系。声明类型调用不证明实际分派；数据边也不等于运行时轨迹。参数与返回值必须有调用点绑定的证据，不能因为发生调用就推断所有参数影响所有返回值。

静态分析不能证明动态调用、外部实现或未知副作用的完整行为。分支、递归、复杂控制流、容器投影或别名等以返回的覆盖情况为准。没有静态路径、没有返回边、`unresolved`、`ambiguous`、`unavailable`、索引部分覆盖均不能改写为运行时不存在。

需验证行为时，结合源码、用户提供的日志及已授权执行结果；代码智能查询本身不执行、导入或验证项目。用户要求实现时，把这些证据用于决定改动位置和相关验证，避免仅输出关系图后停止开发任务。
