# Synthetic live-read / lazy-index resource baseline

2026-10-06：100 定义文件基准与 5000 定义文件探针各一份独立 fixture、各运行一次。
原 100 文件结果保留且未重跑。两者都不是实际企业项目，
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
