# 固定快照 Rust 完整只读请求试验

这个独立进程实际承担 NDJSON 解码、私有快照加载、图发布、源码前后校验、SCC、
依赖优先层级、架构响应构建、完整事实裁剪及 JSON 输出。生产 MCP、项目授权、
忽略策略、写入恢复、数据库和应用生命周期没有迁移，也没有启用原生生产后端。
语法解析、语义绑定和聚合关系事实属于三种实现共同的准备阶段。

```sh
cargo build --locked --release --manifest-path native/intelligence-pipeline/Cargo.toml
/absolute/path/to/colink-intelligence-pipeline-pilot \
  --root /absolute/path/to/frozen/corpus --guard metadata
```

依赖由 Cargo.lock 固定。运行时只访问启动参数固定的真实目录；根及其祖先必须没有
符号链接。进程不导入项目、不执行项目、不启动其他命令、不联网、不写源文件。

私有协议每行一个 JSON，不注册为 MCP 工具：

- `ping`：返回固定 `{protocol:1,scope:"fixed-snapshot-pipeline-pilot"}`。
- `load`：`snapshot` 包含 `version:1`、排序且唯一的 `nodes` 路径、生产格式聚合
  `edges`、`files:[{path,sha256,size}]`。接受 `file_manifest` 别名，不能与 files 同时
  出现。清单必须与 nodes 一一对应；source_root 等其他快照元数据不扩大访问范围。
  两次源码校验均通过才替换已发布图，返回 loaded/edges/files 数量。
- `architecture`：与现有 project_architecture 相同的 path_prefix、max_nodes、
  max_edges、include_type_only 及 max_chars 语义；默认 100/200/false/20000。
- `impact`：root 是现有路径，depth 为 0–5，返回私有 `{affected:{path:distance}}`。
  它不是生产 impact_analysis 的完整符号与引用响应。
- `batch`：queries 中仅包含 architecture/impact，每一项各自执行源码前后校验。
- `stats`：返回累计 CPU 微秒和进程峰值 RSS 字节，架构响应不附带动态时钟字段。

每个查询都完整遍历根下目录并核对所有 .py/.java 源码的增删。任何树内符号链接
均拒绝；源文件不得为硬链接。根及祖先身份、目录遍历期间的版本、文件身份、大小、
mtime/ctime、哈希和路径链接在校验中核对。metadata 模式只在完整版本键相同时
复用已验证的哈希；strict 模式每次重新读取并哈希全部源码。该契约用于冻结副本，
不提供原子文件系统快照，也不替代生产项目授权、来源过滤和恢复机制。

上限为单帧 64 MiB、100000 个源码文件、1000000 条聚合边、单文件 64 MiB、源码
总量 128 MiB、路径 4096 个 UTF-8 字节、目录深度 128、遍历条目 400000、批量
1000 项。错误只返回固定 code，不回显路径、输入内容或异常文本。

裁剪成本按照 Python `json.dumps(...,ensure_ascii=False)` 的默认空格与 Unicode
字符数量计算。files/edges/cycles 最大数组相同时保持生产插入顺序，逐个移除末尾
完整项。采用索引邻接和虚拟响应项成本，裁剪后才复制保留的大循环成员列表；公平
比较应让 Python indexed 参考实现采用相同缓存和预算优化。

这里只记录实现契约。实际规模、差分、通信、源校验和资源占用结论以外部 harness
保存的实测结果为准；编译成功和内核耗时不构成端到端收益证据。
