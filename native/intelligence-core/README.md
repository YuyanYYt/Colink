# 独立 Rust 代码智能核心试点

本模块只接受已经通过现有 Python/Java 前端和源码授权校验的文件关系事实，计算
强连通分量和反向依赖距离。MCP 工具、源码访问、权限、写入恢复、数据库及生命周期
继续由原实现负责。它没有读取项目文件、导入项目、启动命令或联网的接口。

这是实际测试用的独立子进程，不是默认生产后端。没有通过完整查询收益门槛前，
不嵌入现有应用、不改变安装包，也不以 Rust 算法耗时替代用户实际查询耗时。

## 编译与复验

需要 Rust 和 Cargo，依赖由 `Cargo.lock` 固定。可将 `RUSTUP_HOME`、`CARGO_HOME`
和 `CARGO_TARGET_DIR` 指向本项目 `.artifacts` 中的独立目录。

```sh
cargo build --locked --release --manifest-path native/intelligence-core/Cargo.toml
uv run --no-sync python -B scripts/evaluate_rust_core.py \
  --binary /absolute/path/to/colink-intelligence-core-pilot \
  --output .artifacts/new-rust-evaluation \
  --source /absolute/path/to/a/saved/real/source/copy \
  --repeats 50
```

只对明确指定的真实源码副本建立新索引。测试会保留图事实、覆盖限制、准确性对照、
Python/Rust 内核耗时、常驻通信、每次重发图、子进程冷启动，以及包含源码校验和
输出预算处理的 Live 查询耗时。不会执行样例项目。

基准暂以现有 Python AST / Java tree-sitter 事实为共同输入，不声称提高了编译器
语义准确性。SootUp 源码可以作为实际 Java 语料；这不等于已经接入 SootUp 引擎。
SootUp/WALA、Pysa 等深度分析器的适配需要独立验收其构建环境、模型和覆盖状态。

## 私有协议

stdin/stdout 为一行一个 JSON 的版本 1 协议。固定操作为：

- `load`：`nodes` 是唯一文件名数组，`edges` 是文件下标对；验证完后整体替换图。
- `components`：计算强连通分量，保留稳定的遍历顺序。
- `impact`：`root` 是文件下标，`depth` 为 0–5，返回潜在反向文件依赖距离。
- `ping`：读取协议身份。

单行输入最多 8 MiB，最多 50,000 文件、500,000 条输入边；异常只返回固定错误码。
该协议没有注册为 MCP 工具，也没有源码写入能力。它不提供值级传播或运行时证明。

Python 试点适配器为 `scripts/evaluate_rust_core.py` 的 `NativeGraphAdapter`。
结果只在当前私有进程内保留；实际 Live 对照每次重新发布图，避免以未经源码
失效校验的跨请求缓存获取虚假性能优势。
