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
这些结果属于源码与本机协议证据，网页版结果另行记录。
