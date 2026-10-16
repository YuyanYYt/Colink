# build 34 修复基线上的 Java 值传播接入

2026-10-16。以 `12b4a0ecb29784ff875845a61f885ec05b3c602b` 的写入/恢复修复为
基线，在源码中接入有界 Java 值传播。安装版仍为 0.5.0b7 / build 34，本轮不覆盖、
不重启，不启用 Rust 后端，不安装或运行 JDK/Maven/用户项目。

## 使用范围

继续调用已有 `analyze_code`，输入参数保持原样：

- `data_origin`：从返回值或指定值反向追溯参数、定义与传播证据。
- `data_impact`：展开参数或指定值影响的定义、调用与返回值。
- `data_path`：在两个精确值选择器之间查找有限静态路径。
- `combined`：在同一有效上下文返回结构关系与值流证据。

Java 方法使用真实 `symbol_id`，或 `path + 带签名的 qualname`；参数值按名称选择，
返回值可按位置缩小，occurrence 使用一基 UTF-8 字节列。`this` 独立表示，不占用
Java 的业务参数；参数名称兼容 Java 的 `$`。已有关系事实的字符列独立匹配，
避免中文源码前缀或方法限定符导致跨调用丢失。

现有 Tree-sitter Java 把已校验源码降低到非执行的内部流表示，再复用有限的值流
引擎与结果预算。支持参数、局部变量声明/赋值、基础表达式、返回值、明确的静态
调用与同方法显式 this 字段。方法按完整签名和声明起点匹配，不凭名字猜重载。

Java 源码仍由原来的 Live 源码入口按实际哈希和身份读取，查询前后保留授权、
上下文、源码清单校验。Java 使用请求内独立语法树，未送入 Python AST 缓存。
返回声明 `coverage.language=java`、`frontend=java_tree_sitter_ir_v1` 和静态
may-flow 语义，继续保留覆盖、诊断、预算限制及 `absence_proven=false`。

## 明确限制

- 实例方法的静态候选不证明 JVM 动态分派。保留名义方法参数绑定，同时保留实参
  与 receiver 到结果的 unknown 替代路径，附 `JAVA_DISPATCH_PARTIAL`。
- 对象构造、外部实现、对象/容器别名、反射、lambda/方法引用、类型转换、复杂
  控制流和未支持的作用域语义明确 partial/unknown。没有接入 SootUp/WALA 的
  JVM 类型、堆或运行时分析，不声称全部 Java 语义已与 Python 对等。
- 没有方法体或签名位置不匹配时明确不可分析，不创建假返回值来冒充完成。
- 无路径只描述本次有限切片，不能证明运行时不可达。

## 本轮实际验收

只运行小批功能测试，没有运行 pytest 或任何原有回归套件。新建独立源码/索引，
通过真实 `build_mcp.call_tool("analyze_code", ...)` 完成 13 笔调用：8 个 Java
场景与 1 个 Python 简单冒烟场景，最终 9/9 通过。

| 场景 | 实际结果 |
| --- | --- |
| Java 参数、局部赋值、返回值的 data_path | ready，包含赋值及返回传播 |
| data_origin | ready，追溯到真实参数 |
| data_impact | ready，参数影响到返回值 |
| 跨文件静态 helper，调用行有中文注释 | ready，ARG_TO_PARAM 与 RETURN_TO_CALLSITE 齐全 |
| combined | 方法结构和 Java 值流同时返回 |
| if / 未知库调用 | partial，控制流/调用解析诊断分类保留 |
| 赋值覆盖 | 旧参数无该静态路径，新参数有路径，未作运行时不存在断言 |
| 实例方法首参 | 本地首参路径正确；完整影响切片明确绑定实际 value 参数，调用方仍 unknown |
| Python 简单冒烟 | 既有参数到返回值静态路径可用 |

脚本为 `scripts/verify_java_dataflow.py`，只创建新的 `.artifacts/java-dataflow-*`
目录并保留材料。适配器冻结后在同一组场景上完成最终功能验收，回执：
`.artifacts/java-dataflow-final-20261016-01/receipt.json`。
此前通过的 `.artifacts/java-dataflow-functional-20261016-03/receipt.json` 同样保留。

前两次回执也保留：脚本最初将最短 unknown 分派路径当成静态绑定路径，后调整
断言并补查同实例场景的完整影响切片，确认 ARG_TO_PARAM 的真实目标。没有通过
修改引擎来迎合静态路径断言，也没有将受限路径升级为确定结果。

解析适配器另手动核对了声明注解起点、static/实例首参、显式 this 字段、中文列、
分支/循环诊断，以及现有 Spring Boot Application.java 的 main(String[])。
这些是局部功能证据，不代替完整项目或网页运行验收。

## 源码恢复与保护

开始前建立恢复标签：
`checkpoint/colink-build34-before-java-dataflow-20261016T162313`。
源码、原始工作区差异与安装文件哈希保留于：
`.artifacts/java-dataflow-baseline-20261016T162313/`。

用户原有 docs/WRITE_CONTRACT.md 差异及未跟踪样例继续保留。本轮不修改安装包、
权限、用户项目、数据库或恢复记录，不推送 GitHub，不删除生成物。
