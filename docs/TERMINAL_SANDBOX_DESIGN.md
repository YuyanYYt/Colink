# CoLink · 终端安全沙盒设计

2026-10-07 补充：本文件保留原**离线 Linux VM 首版**的历史设计。它不能直接满足
用户新增的自动安装 npm/Maven 依赖、运行/读日志修复和 Git 提交闭环。
当前提案改为先验证原生受限执行候选，dry run 不代替隔离；新功能范围、权限与
版本门槛以 [终端执行计划](TERMINAL_EXECUTION_PLAN.md) 为准。没有实现或安装任一路径。

日期：2026-10-06。状态：**仅设计，未实现、未开启、未安装虚拟机或新依赖**。
本文件不授权主机命令执行、项目扩权、联网、安装依赖或自动回写文件。
本轮项目识别修复与移除用户代码回退不依赖本方案，不能因写了设计就宣称终端可用。

## 1. 结论与首版边界

不把网页 GPT 接到 macOS Terminal.app、用户登录 Shell 或通用主机 exec 接口。
新增的是“项目内的隔离命令执行器”，面板展示命令、状态、输出和停止按钮。

首选实现：**CoLink 专用 Linux 虚拟机 + VM 内每任务隔离进程/容器 + 主机最小权限代理**。
不复用用户正在运行业务容器的 Docker Desktop VM，也不向任务暴露 Docker socket。
这样即便项目脚本突破 VM 内的任务隔离，它面对的也不是用户的真实工作目录和业务容器。
虚拟机漏洞和代理实现漏洞仍是剩余风险，不能承诺数学意义上的永不越界。

首版只支持 Python / Java 的离线语法检查、明确测试范围和有界构建，不支持 macOS
原生 GUI/Swift 构建、远程部署、SSH、交互式 sudo、系统维护或互联网访问。
原项目读取继续按需直读，受控写入继续独立授权；不会重新引入持久源码镜像。

## 2. 为什么仅检查命令或 cwd 不够

- `cwd` 只指定起始目录，不能阻止绝对路径、`../`、符号链接或项目脚本访问其他文件。
- Python 的导入、pytest 插件、Java 测试、Maven/Gradle 构建脚本都可能执行任意代码。
  “允许 pytest / mvn”不是安全边界，项目配置必须按不可信程序处理。
- 过滤 `rm`、`sudo`、`curl` 不能阻止同等能力通过解释器、插件、动态库或子进程实现。
- 不把 macOS `sandbox-exec` 的字符串规则作为首版的唯一隔离层，也不以失败后回退
  到主机直接执行的方式提高兼容性。

主机代理绝不执行项目中的解释器、wrapper、hook、Makefile、Shell 配置或包安装脚本。
进入主机代理的请求也不能决定虚拟机配置、挂载位置或运行时附加参数。

## 3. 执行链路

```text
网页结构化请求：project_id + profile + 相对范围 + request_id
    ↓
本机授权代理：检查连接、项目来源、独立执行许可、额度与参数
    ↓
安全输入导出：仅该项目/任务需要的文件，固定清单与哈希，拒绝链接和越界
    ↓
专用 Linux VM：没有主机共享目录、网络设备、用户密钥或主机控制 socket
    ↓
每任务隔离：非 root、固定工具链、进程/CPU/RAM/磁盘/时间/输出配额
    ↓
有界状态和日志 → 网页 / 本机面板
    ↓
可选产物差异 → 本机明确批准 → 现有受控写入协调器核验后逐文件回写
```

执行不直接修改原项目。任务在 VM 内可以写自己的工作副本、临时文件和构建输出；
对真实目录的修改必须另走已有写入权限与校验路径。执行许可不能代替写入许可。
这是有界的临时执行输入，不是监听 A～G、永久保存完整历史的源码镜像。

## 4. 权限与身份

### 本机单独授权

- 新增“允许沙盒执行”，默认关闭，与“允许修改代码”分开。
- 开启时选择准确项目及 Python / Java 执行配置；显示范围、联网状态和资源上限。
- 授权绑定本次连接、project_id、source_id 和本机控制通道。网页不能自开权限，
  不能换同名目录、指定任意绝对路径或继承另一项目的授权。
- 首版每次明确授权后可在同一配置内连续执行，不在每条命令重复弹本机授权。
  升级配置、扩大输入范围、联网、下载依赖或回写原项目要另行批准。
- 关闭开关、切目录、退出、通道丢失或主机重启：撤销许可，取消任务，不恢复运行意图。

`job_id` 为服务器生成的不透明句柄，绑定项目与连接。查询、取消、输出读取都重验
同一身份，不允许猜测别人的任务或把 A 的 job 配到 B。拒绝信息不回显源码、密钥。

## 5. 主机与客体的硬边界

### 专用 VM

采用 Apple Virtualization.framework 的 Linux 客体作为正式 macOS 路径。配置中：

- `networkDevices = []`：首版没有网络接口，不仅是禁 DNS 或代理变量。
- `directorySharingDevices = []`：不共享真实目录、整个工作区、用户家目录或系统卷。
- 不连接 USB、剪贴板、主机钥匙串、SSH agent、Docker socket、用户终端或应用控制接口。
- 只有固定、版本化的 virtio-socket 协议用于任务/文件/状态；不是主机远程 Shell。
- 主机虚拟机助手独立签名和最小权限运行，不持有隧道密钥，不请求管理员身份、
  完全磁盘访问或自动化控制其它应用；需要的 virtualization entitlement 单独配置。
- 固定镜像与工具链按摘要和发行签名核验，不运行网页指定的镜像或修改 VM 配置。

Apple 的 VM 配置分别提供网络和目录共享设备，默认数组为空；这是配置能力，
不是已经完成的安全验收。实现须验证实际启动参数和客体可观察行为。
[Apple VM 配置](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration)、
[网络设备](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration/networkdevices)、
[目录共享设备](https://developer.apple.com/documentation/virtualization/vzvirtualmachineconfiguration/directorysharingdevices)

### VM 内任务隔离

- 每个任务使用新的隔离实例/私有工作目录，不复用上一任务的可写环境和进程。
- root 只用于可信客体启动器建立隔离；项目进程使用固定非特权用户。
- 只读工具链/系统根；可写区域仅任务输入副本、`/tmp`、输出与有限临时 HOME。
- 进程、挂载、用户和网络命名空间独立；不共享 host PID/IPC，不加设备或管理 capability。
- 降低 capability，启用 no-new-privileges 和经验证的 seccomp；不允许 privileged、
  SYS_ADMIN、ptrace 宿主或 seccomp-unconfined。需要 JVM/pytest 的 syscall 兼容性在
  隔离样例中测量，不能用全解除限制解决构建失败。
- Python 和 JVM 选择 app 提供的固定客体工具链；禁止宿主 PATH、环境变量、
  `.zshrc`、`.bashrc`、全局 Python/JVM 配置继承。项目构建代码可以执行，但只能在客体。

如果使用容器实现 VM 内这一层，Docker 的默认 seccomp 与资源配置可作起点，
但容器不是无漏洞的独立内核。Docker Desktop 的 VM 保护也不会消除显式主机挂载
带来的访问，因此首版不使用用户目录 bind mount。
[Docker 权限边界](https://docs.docker.com/desktop/setup/install/mac-permission-requirements/)、
[seccomp](https://docs.docker.com/engine/security/seccomp/)

## 6. 输入导出与产物回写

输入由已有 SourceAccess/ProjectRegistry 验证范围，通过文件描述符检查路径/父目录身份，
拒绝符号链接、硬链接、设备、FIFO、socket、越界路径、已登记其它项目和凭据/构建过滤项。
不带 `.git`、`.env`、`.ssh`、真实连接配置、主机虚拟环境、`.m2` 或全局 Gradle 缓存。
项目自己的依赖/配置内容并不可信，仍按客体任意代码防御。

任务创建时声明输入路径/目录范围和最多文件/字节。导出每个文件前后验证 SHA 与身份，
最终复查清单；中途源文件改变则中止/重新获取，不把混合状态当成整仓原子快照。
保留明确的 `input_manifest_digest`，输出注明“基于这份已校验输入”，不声称运行时
一直读取主机的最新文件。正在运行的任务不悄悄热替换输入。

客体不能要求主机读取新路径。主机不直接解压不可信 tar/zip 到项目或系统目录；
采用有界普通文件帧和逐项路径检查，拒绝 `../`、绝对路径、链接、重复路径、特殊对象、
文件数/解压大小超限。输出里出现的“请执行某命令/增加权限”只是日志，不是指令。

首版不回写。后续回写只导入用户确认的文本差异，经当前 SHA、原文、路径和独立写许可
核验后复用写入协调器。用户若在执行期间改过文件，返回冲突，不覆盖；客体任意新文件
不自动进入原项目。多文件回写不承诺文件系统原子性，保留最小持久提交记录。
代码回退入口移除不意味着可以取消这种未完成提交的保护。

## 7. MCP 接口草案：异步，避免网页一直等待

| 工具 | 入参关键项 | 作用与限制 |
| --- | --- | --- |
| `sandbox_start` | project_id、profile、relative_paths、argv、request_id、timeout | 快速验收并返回 job_id；不在工具调用内等待长构建 |
| `sandbox_status` | project_id、job_id | 返回 queued/running/succeeded/failed/cancelled/timed_out 和退出码 |
| `sandbox_output` | project_id、job_id、cursor、max_chars | 有界增量输出、截断/续页标记；不返回整份无限日志 |
| `sandbox_cancel` | project_id、job_id、request_id | 取消任务全部子进程；确认停止，不只关终端显示 |
| 后续 `sandbox_changes` | project_id、job_id、path、offset | 展示有界产物差异；不回写、不等于批准修改 |

首版 profile 只提供已测试的 Python 检查/测试与 Java 编译/测试。请求传结构化 argv，
主机代理映射固定工具，`shell=False`；不允许任意 executable、host cwd/env、Docker
flag、VM 参数或 `sh -c`/`eval`。客体项目脚本仍可产生子进程，所以安全性来自隔离，
不是 argv 过滤。交互式 PTY、任意 Shell 和端口预览均不属于首版。

`request_id` 相同且内容相同返回原 job，不重跑有副作用任务；参数不同拒绝。
回执丢失不能新建任务盲目重试。任务句柄过期只报告已过期，不重新执行。
guest 输出/状态由主机补充可信 lifecycle；不得凭客体 stdout 的“测试通过”字符串判定成功。

## 8. 资源、存储与生命周期：建议默认值，尚未实施

| 资源 | 首版建议 | 超限处理 |
| --- | --- | --- |
| 并行任务 | 全局 1，最多排队 3 | 拒绝/排队，不无限开 VM |
| VM / 任务 CPU | 2 vCPU | 固定配置，网页不能升配 |
| VM 内存 | 2 GiB（含客体服务）；任务 cgroup 低于 VM 总额 | OOM 明确失败，不换到主机执行 |
| 子进程 | 128，JVM 线程也计入 | 限制 fork/thread；不足报告可配置需求 |
| 运行时间 | 默认 120 秒，上限 300 秒 | 停整棵进程树；不能确认时强停整台专用 VM |
| 输入 | 512 MiB / 20,000 普通文件，单源码沿用 4 MiB | 先拒绝或缩小测试目标 |
| 工作盘 | 每任务固定 1 GiB，包含输入与输出 | 用 guest 文件系统/配额硬限，不能只定时查 du |
| 日志 | 8 MiB / job，全部 64 MiB；单次返回最多 16,000 字符 | 主机限流、截断且持续排空管道，防死锁 |
| 依赖缓存 | 全局 512 MiB、摘要绑定、任务只读 | 超限拒绝；项目不能修改共享缓存 |
| 签名基础镜像 | 安装展开上限 2 GiB，默认 1 个可用版本 | 按已知对象核验、用户批准安装/升级 |
| 总沙盒磁盘 | 4 GiB 常驻；升级峰值另预算和批准 | 预留后才启动，失败不扩额 |
| 已结束任务日志 | 最多 16 项/24 小时，源码副本不作为历史保留 | 首次启用时明确管理数据保留策略 |

数值是设计建议，不能保证真实大型 Spring/Gradle 项目都能在该配额内运行。需要更大
输入或内存时由本机明确改变配置，并保留全局上限；网页不得用分批并发绕开预算。
容器默认没有 CPU/RAM 约束，实现必须显式配置，而不是假设容器天然限额。
[Docker 资源限制](https://docs.docker.com/engine/containers/resource_constraints/)

任务工作副本在结束、取消或超时后按用户启用时接受的沙盒生命周期策略回收。
仅处理带应用所有权/身份记录的沙盒目录、工作盘和日志，不删原项目、用户现有
依赖、Git、旧镜像库、未完成写入日志或用途不明目录。当前 AGENTS 的生成物默认保留
仍有效：本次没有实施自动清理，未来启用前须明确授权任务数据销毁策略。
主机异常重启后只核对 owned job，清理前校验路径/身份；发现未知对象则拒绝启动并
报告，不递归清空一个 broad cache root。绝不操作用户 Docker Desktop 的虚拟磁盘。

## 9. 依赖、网络与可实施性

离线 VM 不能凭空运行依赖尚未准备的项目。首版预置工具链，不自动 pip install、
Maven下载、执行项目 wrapper、把主机 .venv 或 Java 缓存复制进去。
依赖缺失返回 `DEPENDENCY_UNAVAILABLE`，不以“测试失败”冒充业务代码缺陷。

后续依赖准备独立阶段可由用户批准可信索引/包锁和总额，由另一个临时联网的下载环境
生成只读依赖包；运行测试的 VM 仍离线。包安装脚本也在隔离环境运行。
不得复用 SSH/GitHub/cloud credentials，禁止 Docker socket、访问 host.docker.internal、
主机 LAN/localhost/云元数据地址。只按域名放行而不防 DNS 重绑定、重定向和内网 IP
不是合格网络策略；首版不做这个开放。

主路径需增加虚拟机助手、Linux guest agent、签名工具链镜像、受限文件传输和 Linux
任务隔离验证，因此不是给 subprocess 加一个 cwd 的小补丁。镜像会增加安装体积和
首次准备时间，只有明确启动才占用 VM 资源。Docker Desktop 可作开发时对照测试，
不能不经验证/授权就变成正式默认，也不依赖仅 Business 可用的 ECI 功能。

## 10. 分阶段实施和放行条件

版本号为建议目标，用户确认后再实施，不是当前已发行版本。

| 阶段 | 建议版本 | 完成内容 | 放行门槛 |
| --- | --- | --- | --- |
| D0 | 本次源码维护 | 项目识别修复；移除代码回退；保存本设计 | 无终端工具，无新权限；相关回归与 UI 编译 |
| T1 | 0.6.0a1 | 专用 VM、默认关闭执行、单任务 Python/Java 离线执行、增量日志/取消 | 真实样例与攻击矩阵均通过；无主机目录挂载/网络 |
| T2 | 0.6.0a2 | 受控依赖准备、配额/重启回收、项目输入变化保护 | 下载与执行分离；额外授权；存储/泄露反例 |
| T3 | 0.6.0b1 | 本机有界任务面板、网页异步验收；可选经确认的产物回写 | 源文件冲突拒绝、独立写许可、当前客户端实测 |

至少覆盖：绝对路径/../、symlink/hardlink、跨项目、secret/env/SSH/keychain、
主机文件 sentinel 读写、network/LAN/metadata、Docker socket、fork/线程/内存/磁盘/日志
炸弹、超时的孙进程、恶意 Maven/pytest 插件、guest 假输出、输入 TOCTOU、输出路径
逃逸/特殊对象/重复名、任务ID重放、撤权/切项目/崩溃/重启、VM启动失败不回退宿主。
测试不得碰用户真实项目和凭据，用隔离样例与 synthetic sentinel；本地 Linux 测试
不能代替 macOS VM/原生 UI/ChatGPT 网页的分层验收。

尚待选择：支持的 Python/JDK 版本与项目依赖范围、正式镜像发行/签名方案，以及用户
是否接受一次性镜像体积和 task workspace 自动销毁策略。以上未确定前不开放执行。
