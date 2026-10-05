# Synthetic live-read / lazy-index resource baseline

2026-10-06：100 定义文件基准与 5000 定义文件探针各一份独立 fixture、各运行一次；
共享 fingerprint LRU 修复后、批量指纹/父 FD 池优化后，各仅再执行一次相同 5000 文件
分布的新 fixture。既有结果全部保留且未重跑。所有测量都不是实际企业项目，
不表示 R6、desktop、隧道、网页、模型或整体重构已验收。

## 重现与输出保护

在本 checkout 根目录执行，所有 fixture / 数据库 / JSON / MD 都保留在新建的
`.artifacts/live-benchmark.<random suffix>` 中，不接受其他来源 root，不自动清理。
`--definitions-a` 仅允许 1000～5000，默认 5000；重复请求默认 3 次，上限 5 次。
`--definitions-per-file` 默认 50，仅允许 1 / 50，同时作用于 A、B。
脚本自己的 fixture 安全枚举上限为 6000 个条目；没有改变主线读取/索引/监听预算。
每个子进程默认超时 120 秒，不自动重跑失败样本。

```sh
live_benchmark_uv_tmp=$(mktemp -d .artifacts/live-benchmark.XXXXXX)
UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR="$(pwd)/$live_benchmark_uv_tmp" \
  uv run --no-sync --frozen python scripts/benchmark_live.py
```

脚本也接受 `--output .artifacts/live-benchmark.XXXXXX`，但必须是新的空真实目录。
不要同时把 uv 的 TMPDIR 指到该显式输出目录：uv 会留下锁文件，脚本应拒绝覆盖。
100 文件基准第一次启动因此在生成 fixture / 查询 / 测量前以 `OUTPUT_NOT_EMPTY` 拒绝；
保留该目录作为 uv 临时目录后，使用另一空输出目录完成唯一一份性能测量。

## 100 定义文件基准（原结果保留）

- [完整性能 JSON](../.artifacts/live-benchmark.RpuStG/performance.json)
- [自动生成的短报告](../.artifacts/live-benchmark.RpuStG/summary.md)
- [冷进程详细结果](../.artifacts/live-benchmark.RpuStG/phase-cold.json)
- [新进程重启详细结果](../.artifacts/live-benchmark.RpuStG/phase-restart.json)
- [fixture 元数据](../.artifacts/live-benchmark.RpuStG/fixture.json)

平台为 macOS / arm64。A：5000 个简单独立 Python 函数，分布在 100 个文件，
另有一个包标记与 3 个 Java 文件（3 个类、6 个方法）；B：100 个函数、2 个定义文件。
普通 `large.txt` 为 3,145,728 字节。含少量依赖/构建/缓存/ignore 剪枝占位文件，
初始生成文件总计 3,418,686 字节，没有生成海量依赖或几百 MiB 源数据。

| 操作 | 本次 wall ms |
| --- | ---: |
| 发现候选（冷进程） | 3.216 |
| A 元信息概览 | 4.408 |
| 首次 A 结构请求 | 512.749 |
| 重复 A 结构请求 | 86.732 / 88.234 / 89.026 |
| 修改一个 A 文件，新上下文重建 | 463.603 |
| 3 MiB 普通文件第 1 / 2 页 | 44.822 / 43.715 |
| 停止后修改，同 DB 的新进程请求 | 492.953 |

重复请求中位数 88.234 ms，仅 3 个样本，不报告 P95/P99。
首次解析 104 个 Python/Java 文件；在线编辑和停机编辑后均只重解析 1 个文件、
复用其余 103 个文件的解析事实，但仍对 104 个文件重新绑定关系。
所以“只重解析一个文件”不代表整个结构更新只做一个文件的工作。
新进程会重新读取参与文件进行校验，不是零读取的热缓存重启。

25 项脚本内测量检查通过，两阶段进程退出码均为 0，无 partial / index-not-ready。
相关模块在运行期间没有变更，JSON 留有实际源码哈希和 Git HEAD；这不是 pytest 数量。
首次与重启均发现 A、B 两个默认未启用候选，再由脚本在本机显式启用；没有查询 B。
两阶段 B 的 `SourceAccess.body_reads`、源码/其他正文读取、六张事实表的项目行数均为 0。
不过每阶段实际读取 B 的两个 root-ignore 文件（共 34 字节），并进行元信息对账。
这是“B 正文未读取/未索引”的证据，不是“B 所有文件零读取”。

## 存储、进程与监听边界

- DB 配置 67,108,864 字节（64 MiB），`managed_peak_config` 134,217,728 字节
  （128 MiB），与当前 Runtime 同值；3 倍 DELETE-journal 保留策略使有效页上限为
  44,736,512 字节（约 42.664 MiB）。这是数据库/日志额度，不是 RSS 内存上限。
- 在线编辑后数据库文件 16,158,720 字节，文件系统分配 16,789,504 字节；
  重启编辑后文件/分配均为 16,146,432 字节。操作后 journal / WAL / SHM 均为 0。
  这是实际文件与分配字节采样，未测瞬时磁盘峰值。
- 空闲 watcher 请求 6 个非递归目录：A 的根、`pkg`、`java`、`java/bench`；
  B 的根和 `pkg`。`node_modules`、`.venv`、`target`、`build`、`.artifacts`、
  `.code-context` 与两种 ignore 子树均不在 watch 集或概览中。
- 冷进程空闲时 FD 为 7、当前 RSS 为 45,973,504 字节；首次结构请求后 RSS 为
  79,233,024 字节；冷进程生命周期 RSS 高水位为 94,814,208 字节。
  FD 为 lsof 的整个进程数字描述符采样，包含探针管道；RSS 为 ps / getrusage。
  **未直接测量内核 watch 资源数**，不能用目录数或 FD 数代替 kqueue/FSEvents/inotify
  资源数，也不能据此判定大目录树的 native watch 开销已验收。

直接调用内存 registry、LiveQueries、LiveIndexService、WatchCoordinator；默认请求等待
10 秒。两个独立 OS 子进程验证真实停止/重新打开同一事实 DB 后的变化检测。
没有启动 WorkspaceRuntime 控制通道、菜单栏、隧道、MCP 网络或模型。
冷启动是新进程/空应用 DB；OS 文件缓存没有清空。

Scanner 仪表包含实际 root-ignore 加载，SourceAccess 原有 metrics 不包含该加载；
计数是解码后返回的完整文本字节，不是内核物理 I/O。普通 3 MiB 文件的两页响应各为
20,000 字符且连续，但每页仍完整读取该文件（合计 6,291,456 个解码文本字节）。
进程 CPU 计时包含后台线程，未做大目录、配额边界、事件吞吐或真实企业关系质量验收。

以上提供 R6 判定用的单样本基线，不给出“企业规模性能已通过”或“网页已完成”的结论。

## R6：5000 定义文件目录规模探针

仅新增一次 `--definitions-a 5000 --definitions-per-file 1` 完整运行，warm 仍取默认 3 次。
原 100 文件的 performance.json / summary.md 前后 SHA-256 相同，没有覆盖或重跑。

- [5000 文件性能 JSON](../.artifacts/live-benchmark.4fcphe/performance.json)
- [5000 文件短报告](../.artifacts/live-benchmark.4fcphe/summary.md)
- [5000 文件冷进程明细](../.artifacts/live-benchmark.4fcphe/phase-cold.json)
- [5000 文件重启明细](../.artifacts/live-benchmark.4fcphe/phase-restart.json)
- [5000 文件 fixture](../.artifacts/live-benchmark.4fcphe/fixture.json)

A 为 5000 个函数、5000 个定义文件，另有包标记/Java/配置/ignore/普通文本，概览共 5008
个允许文件；B 为 100 个函数、100 个定义文件。初始生成文件内容合计 3,773,544 字节，
产物目录磁盘分配约 49 MiB，全部仅在本 checkout 的新 .artifacts 目录内，未清理。

| 操作 | 实测 wall ms | index_not_ready / index_partial |
| --- | ---: | --- |
| 发现候选 | 60.713 | 不适用；发现完整 |
| A 元信息概览 | 76.065 | 不适用；发现完整 |
| cold A 结构请求 | 8152.993 | false / false |
| warm A 第 1 / 2 / 3 次 | 8125.882 / 8408.703 / 8207.459 | 均 false / false |
| 修改一个 A 文件，新上下文重建 | 9087.922 | false / false |
| 停机编辑后的新进程请求 | 9163.480 | false / false |

warm 中位数 8207.459 ms，明显慢于原 100 文件基准的 88.234 ms；**脚本功能检查通过
不是交互性能验收通过**。只有 3 个 warm 样本，不能推断可靠 P95/P99。

主线构建等待仍为 10 秒；本轮 cold 的内部 `build_ms` 为 1761.132 ms，没有返回
`BUILD_PENDING`。10 秒作用于 `Future.result(timeout)`，不是整次查询 wall 时间的硬上限；
枚举、上下文与参与文件校验仍另外计时。没有为此提高 DB / managed peak / watch / 等待预算。

每次 warm 的 Scanner code 读取计数均为 20,016，返回的 code 文本字节为 2,457,528；
这不是 OS 物理 I/O 计数。A 指纹缓存条目实测为 4096，而参与的 Python/Java 文件为 5004。
按当前 SourceAccess 的 4096 条目上限与多轮校验流程，循环校验导致缓存抖动是与计数相符
的机制推断；本轮没有增加第二组因果实验，也没有修改主线缓存或校验算法。
单文件编辑与重启编辑均只重解析 1 个文件、复用 5003 个，但仍重新绑定 5004 个文件。

两阶段 B 的正文/code/其他文本读取、`SourceAccess.body_reads`、六张表的 B 行数均为 0；
每阶段仍实际读取两个 B root-ignore 文件、34 字节。25 项脚本内功能/隔离检查通过，
无 DB partial、未就绪或进程超时；这不是 25 个新增 pytest 测试。

- DB 配置 64 MiB、managed_peak_config 128 MiB、有效页上限 44,736,512 字节未改变。
  在线编辑后 DB 文件 26,812,416 字节、分配 27,275,264 字节；重启编辑后文件/分配均为
  26,738,688 字节（25.5 MiB）。操作后 journal/WAL/SHM 为 0；未测瞬时磁盘峰值。
- cold 空闲 RSS 53,936,128 字节；cold 进程生命周期高水位 116,097,024 字节
  （约 110.72 MiB）。空闲 FD 7，watch 非递归目录仍为 6，剪枝样例均不在其目录集。
  5000 文件集中在现有 pkg 目录内，不等于 5000 目录的原生监听压力；内核 watch 资源数
  仍未直接计量，不能拿目录数或进程 FD 冒充。
- 接任务时 HEAD 为 bf89fa0，准备期间主线推进；实际运行前后 HEAD 均为
  b334c3a14bf47f9223f91541b8b0ec1de9b58177，相关源码哈希在测量期间未变。
  与原基准相比，所记主线组件哈希相同，benchmark 脚本与 uv.lock 哈希不同；所以不是
  固定 HEAD 的严格受控 A/B 实验。锁文件不是本任务修改的文件。

重现该文件分布使用新的输出，不要复用已生成目录：

```sh
live_benchmark_uv_tmp=$(mktemp -d .artifacts/live-benchmark.XXXXXX)
UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 TMPDIR="$(pwd)/$live_benchmark_uv_tmp" \
  uv run --no-sync --frozen python scripts/benchmark_live.py \
  --definitions-a 5000 --definitions-per-file 1
```

没有执行网页、desktop 或隧道验收，也没有修改主线组件、LOG、CHANGELOG、提交或清理。

## Backend 共用 fingerprint LRU：一次修复后测量

本轮仅修改 `source_access.py` / `live.py`，新增纯元信息 `fingerprint_cache.py` 与相关测试；
脚本只增加该模块源码哈希与全局缓存统计，不改变 fixture、请求流程、索引或监听预算。
旧 5000 文件探针的 performance.json / summary.md 前后 SHA-256 相同，原约 8.2 秒
warm 负面证据仍在上一节，原 100 文件基准也没有重跑。

- [修复后性能 JSON](../.artifacts/live-benchmark.T69y6r/performance.json)
- [修复后短报告](../.artifacts/live-benchmark.T69y6r/summary.md)
- [cold 明细](../.artifacts/live-benchmark.T69y6r/phase-cold.json)
- [新进程重启明细](../.artifacts/live-benchmark.T69y6r/phase-restart.json)
- [相同规模新 fixture](../.artifacts/live-benchmark.T69y6r/fixture.json)

执行一次 `--definitions-a 5000 --definitions-per-file 1`，默认 warm 3 次。A 仍为
5000 函数 / 5000 定义文件，另有包标记与 3 个 Java 文件；B 为 100 函数 / 100 定义文件。
初始文件正文总计 3,773,544 字节，普通分页文件仍为 3 MiB，没有扩大数据规模。
实际运行前后 HEAD 均为 `55fa700d709a73de2f3210ed000cfa4d386f0fea`，使用本轮未提交补丁；
所记源码在运行期间没有变化。与旧 5000 文件运行相比，live / source_access / policy /
benchmark 哈希变化，并新增 fingerprint_cache；LiveIndex / Scanner / Watch / registry /
解析器与 uv.lock 的所记哈希相同。这不是固定 HEAD、清空 OS 缓存的严格受控 A/B 实验。

| 操作 | 修复后 wall ms | index_not_ready / index_partial |
| --- | ---: | --- |
| 发现候选 | 64.075 | 不适用；发现完整 |
| A 元信息概览 | 71.615 | 不适用；发现完整 |
| cold A 结构请求 | 5160.269 | false / false |
| warm A 第 1 / 2 / 3 次 | 4012.522 / 4029.440 / 4006.032 | 均 false / false |
| 修改一个 A 文件，新上下文重建 | 4949.217 | false / false |
| 停机编辑后的新进程请求 | 6692.311 | false / false |
| 3 MiB 普通文件第 1 / 2 页 | 45.104 / 42.631 | 不适用 |

warm 中位数 4012.522 ms，旧同分布中位数为 8207.459 ms；**约 4 秒仍不是交互性能通过**。
每次 warm 的 Scanner code / other_text / root_ignore 读取与返回字节均为 0；原同分布每次
warm 的 code 读取为 20,016 次。这说明本轮不再重复读取不变正文，不代表没有元信息 I/O。
冷启动首次读 5004 个 code 文件；停机后新进程缓存为空，仍读 code 5005 次进行变化校验。
在线与停机编辑均只重解析 1 个文件、复用 5003 个解析结果，仍重绑定 5004 个文件。
10 秒构建 Future 等待与 120 秒子进程保护均未提高；两阶段退出码 0，无 partial / 未就绪 /
超时，25 项脚本功能检查通过。这些检查不替代性能、网页或企业规模验收。

### 缓存预算与集成 API

每个 `LiveQueries` 仅拥有一个 `fingerprint_cache`，所有初始、刷新与 registry lazy 来源
都通过 `SourceAccess.attach_fingerprint_cache(cache)` 附加它。独立 SourceAccess 默认仍是
4096 条目。`source._fingerprints` 现在是 per-source 元信息视图，`len` 只统计本来源；
不可再把它当作 OrderedDict 或直接赋值下标。

- `FingerprintCache(max_entries=50_000, max_bytes=32*1024*1024)` 同时强制全局条目与字节额度；
  可下调测试额度，不允许超过上述硬配置上限。键为 `(source_id, relative_path)`，值仅为
  6 个版本整数与 SHA-256；没有正文或新 SQLite。
- `get(source_id, path)` 返回不可变 `(version, sha256)` 或 None；`put(...)` 返回是否保留。
  `discard(source_id, path)` / `drop_source(source_id)` 只移除指定文件 / 来源条目。
  `retain_sources(source_ids)` 保留合法命名空间，拒绝被撤权旧访问器的晚到 put；刷新/单来源
  失效不清掉其他 active 项目。`stats()` 返回计数副本，不返回路径或来源集合。
- charged metadata 为每条目 512 字节容器/整数余量 + source ID / 相对路径 / hash 的 UTF-8
  长度；每个授权来源另计 128 + 64 = 192 字节。**这是估算额度，不是实际 RSS 硬上限**。
- 缓存自身线程安全；缓存锁内不调用 SourceAccess、文件系统或调用方回调。来源保持 root
  身份、ignore、nofollow、两次 stat 与版本/内容 hash 校验；变化不能复用旧 hash。
- `LiveQueries.clear()` 清本 backend 的上下文与指纹条目，保留授权集合；`close()` 先撤销
  所有缓存 put，再清理条目并关闭原索引服务，不清其他 backend 的缓存或读取源码正文。

warm 后全局为 5007 条（A 5007、B 0），charged bytes 3,293,993，含两来源绑定 384 字节，
淘汰 0；cold 阶段结束、分页与变化检测后为 5008 条 / 3,294,642 字节。全局上限始终为
50,000 / 33,554,432 字节，未给每个项目各自扩大到 50,000。

### 存储、隔离与资源

B 两阶段的源码/其他正文读取、SourceAccess body_reads、六张事实表 B 行数均为 0，
但每阶段仍读两个 B root-ignore 文件、34 字节，不能说 B 全部文件零读取。
DB 配置仍为 64 MiB，managed_peak_config 128 MiB，有效页上限 44,736,512 字节。
在线编辑后 DB 文件 26,812,416 字节 / 分配 27,275,264；重启后均为 26,738,688 字节。
操作后 journal/WAL/SHM 为 0；不代表实测瞬时磁盘峰值。

cold 空闲 RSS 53,952,512 字节，生命周期 RSS 高水位 124,928,000 字节（约 119.14 MiB）；
重启进程高水位 109,953,024 字节。空闲 FD 7，非递归 watch 目录 6，依赖/构建/缓存/
.artifacts/.code-context/ignore 子树均剪枝。5000 文件仍集中在 pkg，不代表 5000 个目录；
未直接计量内核 watch 资源。RSS 是整个进程采样与高水位，32 MiB charged metadata 与
128 MiB managed DB peak 都不能作为进程内存上限。

### 相关验证记录

仅运行 fingerprint cache / SourceAccess / LiveQueries / ReadContext / LiveIndex 的相关测试，
151 passed in 3.47s。覆盖跨来源键隔离、LRU/bytes 淘汰、变化 hash、nofollow/ignore、
来源撤权和替换不影响 B、64 来源共享同一预算、4100 文件两次上下文校验不再读正文，
以及缓存锁不执行 I/O/迭代回调。未跑全量或修改其他主线组件来凑绿。

实际 pytest 记录（已用的 basetemp 保留，不能复用此命令重建它）：

```sh
UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=.artifacts/fingerprint-cache-validation.ddrtRL \
uv run --no-sync --frozen pytest -q -p no:cacheprovider \
  --basetemp .artifacts/fingerprint-cache-validation.ddrtRL/pytest.OPfjxo \
  tests/test_fingerprint_cache.py tests/test_source_access.py tests/test_live.py \
  tests/test_read_context.py tests/test_live_index.py
```

相关 7 个 Python 文件的 Ruff check / format --check 通过。
基准输出、UV 临时目录与独立 pytest 目录均保留，未提交、未清理；仅提供本机 synthetic
组件与资源证据，尚未完成整体重构、受控写入、desktop、隧道或网页验收。

## 批量指纹与有界父 FD 池：一次独立优化后测量

本小步仅改 SourceAccess / LiveQueries / LiveIndex 与三个对应测试，以及本脚本/文档。
没有接 write barrier，没有改 coordinator / store / file_mutation，没有联网、全量测试、
提交或清理。原 4.01 秒和 8.2 秒报告的 performance.json / summary.md SHA-256 前后相同。

- [批量优化性能 JSON](../.artifacts/live-benchmark.yxF9lx/performance.json)
- [自动短报告](../.artifacts/live-benchmark.yxF9lx/summary.md)
- [cold 明细](../.artifacts/live-benchmark.yxF9lx/phase-cold.json)
- [重启明细](../.artifacts/live-benchmark.yxF9lx/phase-restart.json)
- [新 synthetic fixture](../.artifacts/live-benchmark.yxF9lx/fixture.json)

只执行一次 `--definitions-a 5000 --definitions-per-file 1`，默认 warm 3 次。生成规模与前次
相同：A 5000 定义文件 / 5000 函数，B 100 定义文件 / 100 函数，少量 Java、剪枝占位文件与
普通 3 MiB 分页文件；初始内容总计 3,773,544 字节。不是实际企业代码或海量目录/依赖样本。
实际运行前后 HEAD 均为 `b80dc1aba17a0c47d94f37b00951fcc399d6e36d`，使用本轮未提交补丁，
所记模块运行期间没有变化。与前次相比仅 SourceAccess / LiveQueries / LiveIndex / benchmark
的所记源码哈希不同；shared cache、Scanner、registry、解析器、watch、uv.lock 哈希相同。
仍不是固定 HEAD、清空 OS 文件缓存的严格受控 A/B 实验。

| 操作 | 本轮 wall ms | index_not_ready / index_partial |
| --- | ---: | --- |
| 发现候选 / A overview | 63.853 / 71.590 | 不适用；发现完整 |
| cold A 结构请求 | 2738.470 | false / false |
| warm A 第 1 / 2 / 3 次 | 744.080 / 753.393 / 733.328 | 均 false / false |
| 修改一个 A 文件，新上下文重建 | 1600.128 | false / false |
| 停机修改后的新进程请求 | 2671.627 | false / false |
| 3 MiB 普通文件第 1 / 2 页 | 46.981 / 46.166 | 不适用 |

warm 中位数 744.080 ms，前次为 4012.522 ms。本轮三个 synthetic warm 样本均 subsecond，
且 code / other_text / root_ignore 读取计数与返回字节均为 0；不能据此推断企业项目、P95/
P99 或网页延迟通过。cold、变化重建和重启仍超过 1 秒。10 秒 Future 等待、120 秒子进程
保护、DB 64 MiB / managed_peak_config 128 MiB 均未提高。25 项脚本功能检查通过，两个
独立进程退出码均为 0，无 partial / 未就绪 / 超时。

### 本轮 API、安全和额度

- `SourceAccess.fingerprint_batch()` 是 contextmanager，yield 当前 source；在范围内调用
  原 `source.fingerprint(path)`。正文读取仍走原 `read`，不通过父池缓存或复用正文。
- 批范围复用 nofollow 父目录 FD，保留每项完整祖先栈，合计最多 64 父项、128 retained
  directory FD（计入根/祖先，并为退出授权检查另留根栈余量），不是“64 个叶 FD”冒充额度。
  深的相对父链不能纳入池时，回退原单路径安全打开；该回退的临时单路径栈不属于池额度，
  所以 128 也不是整个进程/任意深链的 FD 硬上限。根栈/嵌套本身超预算时安全拒绝。
- 每个参与文件仍用当前缓存中的版本 + hash，并执行两次 leaf stat；缺失/变化走原完整
  内容校验或失效，不跳过参与文件。LRU 淘汰和批末均验证被保留的父链接版本。
- 批前/后检查 root、ancestor、ignore 版本、source ID 和当前范围；进入/退出虚拟 root_fd
  保留 registry 的当前工作区/来源/启用授权检查。路径、父 symlink、替换、scope 或 ignore
  变化拒绝整批。目录版本检查是保守的，批内相关目录元信息变化也可能要求重试；不是
  文件系统原子快照，也不承诺检测两次观察之间所有可能的外部编辑。
- 嵌套使用独立池、共享该 source/thread 的合计额度；线程状态独立，Source 锁串行来源
  操作。异常、失效及正常退出均关闭所有批内 FD，不保留跨请求父 FD 或正文。
- `LiveQueries.fingerprint_batch(project_id, source=None)` 包装来源批范围，入/出双检当前
  backend 来源对象和授权；ReadContexts.validate 与 LiveIndex 的上下文校验都挂入它。
  可复用 parse 的 hash 校验在提取循环获取 Index 锁前批量完成，发布前 manifest/context
  guards 保留。锁顺序仍是 Source -> Registry -> Cache，cache 锁不做 I/O，不获取
  coordinator.lock；本轮没有添加读写 barrier。

shared cache 上限仍为全局 50,000 / 33,554,432 estimated charged metadata bytes，不是 RSS。
warm 后 5007 条、charged 3,293,993 字节、淘汰 0；批池 metrics 在 cold 阶段为峰值 2 父项 /
12 retained FD、60 个父/祖先 FD 打开、30 次批范围，回退/淘汰 0；重启为 2 父项 / 12 FD、
12 个父/祖先打开、7 次批范围。这里是池元信息计数，不是内核 watch 资源数。

### 资源和验证

两阶段 B 正文读取、body_reads、六张事实表 B 行数均为 0，批范围数为 0；每阶段仍实际
读取两个 root-ignore 文件、34 字节。在线/停机变化均只重解析 1 个文件、复用 5003 个，
仍重绑定 5004 个。在线后 DB 文件 26,812,416 字节 / 分配 27,275,264；重启后两者均为
26,738,688 字节；采样 journal/WAL/SHM 为 0，不是实测瞬时磁盘峰值。

cold 空闲 RSS 54,198,272 字节，生命周期高水位 126,861,312 字节（约 120.984 MiB）；
重启高水位 110,460,928 字节。空闲进程 FD 7，非递归 watch 目录 6，依赖/构建/缓存/
.artifacts/.code-context/ignore 子树剪枝仍有效；没有计量内核原生 watch 资源。
charged cache、FD 池额度和 managed DB peak 都不能作为整个进程 RSS 上限。

相关五个模块最终 **173 passed in 3.07s**（此前既有三模块小回归 64 passed），没有运行
全量。专项检查实际 os.open/close 记录、64 父项 / 128 FD 淘汰、130 层相对链回退、嵌套/
线程隔离、异常收尾、父链接替换与移回、根/祖先/ignore/授权变化、最后一个 leaf 后的
registry/来源对象复核，以及 batch 不持 Index 锁。相关 7 个 Python 文件 Ruff check /
format --check 通过。实际 pytest 记录如下，已经使用的独立 basetemp 不得复用：

```sh
UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=.artifacts/fingerprint-batch-validation.mMXD9o \
uv run --no-sync --frozen pytest -q -p no:cacheprovider \
  --basetemp .artifacts/fingerprint-batch-validation.mMXD9o/pytest.8nXjF9 \
  tests/test_source_access.py tests/test_live.py tests/test_live_index.py \
  tests/test_read_context.py tests/test_fingerprint_cache.py
```

基准新输出、UV 临时目录和 pytest 目录均保留。本轮到此冻结实现并交回 ownership；
subsecond 结论只针对上述三个本机 synthetic warm 样本，不代表写入主线、desktop、
隧道、网页或整体重构已完成。
