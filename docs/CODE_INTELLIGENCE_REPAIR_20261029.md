# 网页实测后的代码智能修复

本轮修复以 `0.5.0b7 / build 35` 为基线，构建号提升为 36。范围来自实际网页测试
和随后的独立源码核验；原项目仅用于只读对照，不修改或执行其业务代码。

## 接口保持与参数组合

默认 `analyze_code` 继续隐藏 mode，省略时按选择器路由。没有增加 MCP 工具或一组新模式；
Live 只读、带写协调器、完整工作区仍分别注册 9、19、40 个工具。

调用路径与数据路径现在都可以请求 `include=["calls","source"]`。ownership、calls、
dependencies 在非上下文主分析中作为 `context` 返回，source 仍为 selector 所指源码。
主分析的 mode、path_found 与结论不被附加上下文替换。各部分共享同一授权来源、事实发布、
响应字符预算和剩余计算期限。

已知请求约束用 `INVALID_ANALYSIS_REQUEST` 加具体原因报告，例如缺少值选择器、错误端点、
超出分析范围或符号不存在；响应容量不足继续使用 `ANALYSIS_RESPONSE_BUDGET_EXCEEDED`。
错误不回显用户源码或令牌。

公开 schema 要求非空 selector，避免只有 project_id 的请求在公开校验通过后必然失败。
内部保留的旧 mode 仍供既有兼容调用使用；严格按默认 schema 的客户端不应发送未声明字段。
本轮不恢复公开 mode，也不将源码读取完成的继承问答冒充为高级关系入口已经验收。

单个具体值复用已有 direction：incoming 查来源，outgoing 查影响，both 返回联合上下文。
两个具体值仍形成起点到目标的定向数据路径；显式兼容策略保持其原有语义。

## 调用关系的执行上下文

Python 函数头部的装饰器表达式、默认参数和实际求值的注解 CALL 归到实际求值的外层 owner。
在 outer 内定义 nested 时，真实发生的默认值求值仍可作为 outer 的调用，不统一丢弃 definition。
类型引用和装饰器声明的归属继续保留，不能与执行期调用混为一类。

延迟 lambda 体携带 execution_context=deferred_lambda 与匿名 execution_scope；引用检索仍能
返回这些源码证据。普通调用路径不扩展未证明执行的 lambda 或 type_only 边，而在
not_traversed 中保留它们，标记 partial。直接执行的 `(lambda: ...)()` 保留可遍历语义；
位于外层延迟 lambda 内的立即调用仍不会提前成为外层创建者的执行路径。

普通 body 与 Java/TypeScript/JavaScript 关系省略新增默认字段，旧关系记录仍可解码。
PARSER_VERSION 提升为 facts-v5，使旧派生关系按需重建，不清空原始源码、配置、恢复库或当前数据库。

复杂高阶异步传播仍允许返回 partial/unknown；不能以未找到静态路径证明运行时不可达，
也不会为了使测试“找到路径”而伪造外部调用或回调传播。

## 裁剪与续查

符号的 parameters、signature 等事实内部不被逐项裁掉。完整的最小事实超过预算时，
明确返回容量错误，不把残缺参数列表伪装为完整签名。

只有实际删减的分区才记录 truncated、truncated_reason 与 clipped_fields 计数；
未改变的空调用图不因其他分区裁剪而被标为截断。尽量保留各请求图的有用完整边，
硬预算仍优先于完整性。图端点、路径见证与保留边保持一致。

next_offset_scope 说明符号或候选分页范围。分区的 continuation 提供 selector/include 等
公开参数片段，并明确复用原 project_id/snapshot；它不是原 offset 下的关系边续页。
无法直接继续图边时，建议缩小选择器或专门请求对应分区，不编造游标或隐藏 mode。

## 验收边界

验证分别记录解析与关系语义、公开参数组合、预算与来源校验、独立源码副本重放、
安装包身份/签名/运行、以及真实网页版 GPT 调用。源码通过和本机 SDK 重放不能替代网页验收。
本轮构建与网页结果以最终交付回执为准，生成的实验和构建材料保留。

本轮独立冻结源码的 24 个相关测试文件共 931 项通过；两个本机 demo 通过。
真实项目的 21 个源码请求已在新索引上重放，原来 C15/C18 的 include 组合错误不再出现，
五处真实调用引用与源码一致。数据流受限处仍明确 partial/unknown，未将其改成虚假的完整路径。
这些结果属于源码与本机协议证据。

## 安装与网页验收记录

修复源码提交为 `809a3abab04f701003d899bbfa6cc972a2d96185`。已覆盖安装
`0.5.0b7 / build 36`，安装包与安装目录的文件树一致，105 个 Python 模块与提交源码
一致；严格签名检查、包迁移检查和安装后的基础运行通过。既有项目、连接配置和只读
授权范围保留，未开启写入或开发能力。旧 build 35 应用副本已移入废纸篓，可恢复。

安装版使用新索引重放 21 项原始只读源码请求及 9 项对照请求，均无工具错误；
当前项目任务状态另在网页查询。原项目三个对照源码文件在验收前后哈希一致。

通过 Chrome 原生界面刷新 ChatGPT 的既有 CoLink 工具定义后，建立新会话执行两轮
只读验收。网页会话共报告 14 次 CoLink 调用、其中 6 次 analyze_code，0 次工具错误，
未执行写入、终端或项目代码。已观察页面完成并保存会话文本；这些文本不是原始协议
包导出，原失败请求的参数与主要字段另由上述安装版 SDK 重放交叉核对。

| 网页检查 | 观察结果 |
| --- | --- |
| chat 到 run_agent_call，附 calls/source | call_path、partial，直接路径成立；不再报 INVALID_STRUCTURE_QUERY |
| request 到 chat 返回值，附 source/calls | data_path、partial，absence_proven=false；异步及高阶调用仍报告未知 |
| 文件结构分析，max_chars=6000 | file_context、partial；裁剪计数、分区原因、symbols 续查范围保留 |
| run_agent_call 引用 | 5 处静态 CALL，与源码 86、95、104、113、122 行一致；定义是额外文本命中 |
| chat 到 get_agent | call_path、partial，不把延迟 lambda 自动扩展为已执行路径 |
| 单值来源及影响 | 未传 mode，以 incoming/outgoing 分别路由到 data_origin/data_impact |
| ChatRequest 继承问答 | 通过源码确认 ApiModel 父类及继承配置；不冒充高级关系图入口验收 |
| 当前项目任务摘要 | recent_task=null，正确报告无保留任务起点，不新建任务或以 Git Diff 替代 |

诊断汇总中的 21 包含 1 项 resource_budget 标记；裁剪计数中的 diagnostics.before=20
指裁剪前的源码分析诊断数量，两者统计口径不同。返回的预算和覆盖提示应保留，
不能把 partial 当作工具调用失败，也不能把没有静态路径当作运行时不可达。

本轮结果支持继续保留自动入口与隐藏 mode；没有恢复该公开参数的必要。此次验收
不构成自动路由与显式模式的统计成功率对照，也不宣称复杂异步值流已完整支持。
