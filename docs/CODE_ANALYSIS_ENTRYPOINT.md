# 统一源码读取与分析入口

2026-10-29 开发源码：常用 `analyze_code` 按精确选择器路由，不再要求选择 mode；
原有显式 mode 继续在服务端严格校验并兼容。结构分析新增有界 TypeScript/JavaScript
前端，值传播仍限 Python/Java。当前改动未更新正在使用的安装版，见
[本轮实现与验证边界](CODE_INTELLIGENCE_20261029.md)。下文 build 32/33 的入口删除
记录保留其历史含义。

当前 Live 主要使用 `read_code` 与 `analyze_code`。前者按需读取已保存正文，后者
查询结构、数据传播及任务 Diff，非 diff 分析可附带目标源码。Live 只读工具为
9 个、带写协调器为 19 个、完整工作区为 40 个；`tools/list` 与实际注册一致。
历史 Mirror 保持 15 个原只读工具及原 snapshot/previous 语义。
本轮双入口整理阶段先达到 39 个，随后按用户要求加入一个 `write_batch`，当前
总数为 40 个。批量写入沿用任务授权、逐项恢复记录和提交保护，执行中断可部分
完成，不构成多文件原子事务；使用方法见 [读取与写入指南](LIVE_USAGE.md)。

## 已删除的 Live 入口

| 已从 Live 删除的工具 | 当前入口 | 参数对应 |
| --- | --- | --- |
| read_file | read_code · file | path/start_line/end_line/char_offset 放入 request |
| read_symbol | read_code · symbol | symbol_id 或精确 path + qualname 放入 request |
| search_code | read_code · search / text | query 等放入 request；以绑定上下文的 cursor 续页 |
| symbol_search | read_code · search / symbol | query 等放入 request；kind 筛选改为 symbol_kind |
| get_call_graph | analyze_code · call_graph | symbol_id → selector；depth → max_depth；保留 direction |
| get_class_graph | analyze_code · class_relations | symbol_id → selector；depth → max_depth；保留 direction |
| get_file_dependencies | analyze_code · file_dependencies | path → selector.path；保留 include_type_only |
| get_diff | analyze_code · diff | 可选 path → selector.path；保留 baseline/detail/offset/limit |

八个名字已取消 Live 注册，缓存旧定义的直接调用也返回 Unknown tool。`live` 和
`workspace` 已移除 `--legacy-analysis-tools`，`build_mcp` 没有恢复旧 schema 的开关。
更新应用后在客户端刷新工具；本机更新不能清除旧对话中的模型记忆。Mirror 的
原名字与底层图查询算法仍保留，不能把 Live 删除误解为删除历史镜像语义。

build 32 当时只是把四个旧分析入口从默认发现隐藏，仍可调用，也能显式恢复发现。
build 33 完成实际删除，并统一四个源码读取入口；历史 build 32 收据证明的是当时
的发现集合和兼容行为。

`find_references` 返回包含导入与类型引用的静态证据，不能用 callers 代替。
`get_impact_analysis` 表示保守的反向文件依赖影响，`data_impact` 表示选定值的
可能传播；两者语义不同。全项目架构、外部依赖保留专用工具。`execution_*` 和
`terminal_*` 的执行、权限与恢复路径没有合并。

## read_code：精确读取与搜索

顶层字段是 `project_id`、`request`、可选 `snapshot`、`max_chars` 与 `max_ms`。
`request` 按 mode 使用独立严格结构，不接受无关字段或隐式类型转换。省略
snapshot 时首次读取建立上下文；同一轮后续读取、关系查询及续页复用返回的
snapshot。已知文件可直接读取，不要求先调用 analyze_code 或完成结构索引。

| request.mode | 必要字段 | 使用方法 |
| --- | --- | --- |
| file | path | start_line/end_line 指定物理行，char_offset 继续被截断的行 |
| symbol | symbol_id 或 path + qualname | 精确读取定义；重复定义返回 candidates，不任取第一个 |
| search | kind=text 或 symbol，query | 文本或定义搜索；可加 context_lines=0..3 的窄正文 |
| batch | items | 最多五个显式文件范围，整批共享预算 |

读取已知文件：

```json
{"project_id":"已授权项目","request":{"mode":"file","path":"service.py","start_line":1,"end_line":80}}
```

搜索定义，并按返回的符号身份精确读取：

```json
{"project_id":"已授权项目","request":{"mode":"search","kind":"symbol","query":"run","exact":true,"path_prefix":"src","limit":20}}
```

```json
{"project_id":"已授权项目","snapshot":"上一查询返回的标识","request":{"mode":"symbol","path":"src/service.py","qualname":"Service.run"}}
```

定义搜索支持 symbol_kind、path_prefix、language；文本搜索支持 path_prefix。
搜索使用返回的 `next_cursor` 作为 request.cursor，保持同一 snapshot、查询和
过滤条件；cursor 是不透明校验标识，不能推测 offset 或挪到其他查询。
符号正文按 `line_offset`、`max_lines`、`char_offset` 读取；歧义候选使用
`candidate_offset` / `candidate_limit`，与正文行分页不同。

少量显式范围共享一次响应：

```json
{"project_id":"已授权项目","request":{"mode":"batch","items":[{"path":"service.py","start_line":10,"end_line":50},{"path":"helpers.py","start_line":1,"end_line":40}]},"max_chars":12000}
```

batch 返回的 next_cursor 包含 item_index/start_line/char_offset。续读时保持原
items 和同一 snapshot，将位置放入 request.cursor；它不代表整个项目批量读取。
file 范围默认 200 行、最多 1000 行；symbol 的 max_lines 默认 200、最多 1000。

各模式默认共享 20,000 字符、3000 毫秒预算；字符范围 1000..50,000，时间范围
100..10,000 毫秒。字符预算包含 JSON 序列化后的转义文本与元数据，搜索还有
扫描字节限制。截断按准确位置续读，不跳过未返回的匹配或残余正文。读取仍校验
授权、路径、文件身份、链接与上下文；失败时释放本次新建的上下文，用户传入的
已有上下文保留，容量耗尽与失效分开报错。

## analyze_code：关系与可选正文

常用查询只提供选择器：文件得到文件上下文，精确符号得到符号上下文；一个具体值
得到来源与影响，两个具体值得到值路径，两个精确符号得到有向调用路径。仅选择
文件或方法不会启动值流，重复定义仍须消歧。

```json
{"project_id":"已授权项目","selector":{"path":"service.py","qualname":"Service.run"},"include":["source"]}
```

```json
{"project_id":"已授权项目","selector":{"path":"service.py","qualname":"Service.run","value":{"kind":"parameter","name":"value"}}}
```

旧 mode 从默认发现 schema 隐藏；需要明确方向/关系的既有调用及任务 Diff 仍可使用
下面的兼容写法。非法 mode 与未知字段仍被拒绝，不静默改成另一个查询。

双向调用图与选定方法源码：

```json
{"project_id":"已授权项目","mode":"call_graph","selector":{"path":"service.py","qualname":"Service.run"},"direction":"both","max_depth":3,"include":["source"]}
```

文件依赖过滤类型专用关系：

```json
{"project_id":"已授权项目","mode":"file_dependencies","selector":{"path":"service.py"},"direction":"incoming","include_type_only":false}
```

file_context/symbol_context 的 include 可选择 ownership/calls/dependencies；所有
非 diff 模式还可追加 source。combined 返回类型明确的结构与数据流关系。
source 字段是选定文件或精确符号的有界读取结果，仅附带当前 selector，不自动
收集全图节点或 target 的正文。重复定义只返回候选，先消歧再读实际定义。

关系和正文来自同一有效 Live 上下文，共享 max_chars 与剩余 max_ms；附正文
要求 max_chars 至少 3000，默认仍为 12,000。剩余正文预算不足时读取 source
的部分状态与续页信息；需要更多正文再定向调用 read_code。图、正文或索引的
部分结果不能当作完整证据。索引等待和必要安全校验不保证在计算预算内完成，
max_ms 不能解释为整条请求的严格墙钟期限。M1/M2 边界分别见对应文档。

合法请求的结果大于预算时，先裁剪完整事实并保留可继续的符号页；最低限度结果
仍装不下时返回 `ANALYSIS_RESPONSE_BUDGET_EXCEEDED`，不会将容量不足误报为
`INVALID_STRUCTURE_QUERY`。`index_coverage.selected_structure` 只说明所选文件
的结构提取状态；全局 `index_partial` 对引用、调用路径的限制仍然有效。

查看最近仍保留的写入任务相对于起点的修改：

```json
{"project_id":"已授权项目","mode":"diff","selector":{"path":"service.py"},"detail":"patch","limit":20}
```

diff 可省略 selector 获取分页 summary，不支持 include=source。baseline=previous
指当前仍保留的写入任务原始文件，不能解释为任意外部修改或 Git 历史。
NO_TASK_BASELINE 表示历史不可用、analysis_status=unavailable，不能当成零变化。
baseline=empty 必须显式传入。该模式复用任务文件验证、恢复对象和 Live 前后
校验，不构建/访问结构索引。输出预算只裁剪 patch 文本，保留变更元数据和
扫描分页位置；无法容纳元数据时缩小 path/limit 或提高 max_chars。

## 验证与历史证据

当前验证以底层图查询为算法基线，覆盖三种图、三个方向、三个深度及正常/截断
预算共 54 组，并验证类型专用过滤、Diff 起点/补丁/分页、旧上下文与 scope 拒绝。
工具集合测试检查发现与实际注册一致、八个旧 Live 名字不可调用、CLI 开关拒绝，
以及 Mirror 保持十五工具。新读取约束、预算与续页另有专门用例。

build 32 的选择性内存 AST LRU 继续保留：源码至少 32 KiB 且原生解析至少 5 ms
才准入，工作区最多 4 个 AST / 32 MiB 估算驻留；每次重新计算数据流与调用上下文。
五分钟滑动窗口及索引关闭一并释放缓存，见 [M2 边界](CODE_INTELLIGENCE_M2.md)。
历史收据位于 `.artifacts/analysis-upgrade-20261011/`。本地测试、安装运行与真实
网页验收分别记录；工具减少、测试通过或缓存性能不证明网页延迟、模型 Token
或电池收益。生成的证据与用户持久数据保留。
