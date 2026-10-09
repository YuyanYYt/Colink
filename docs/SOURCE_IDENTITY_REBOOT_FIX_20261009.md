# 工作区重启身份修复 · build 23

## 原因与实现

本机于 2026-10-09 14:52 重启后，既有目录的 `st_dev` 从 16777233 变为 16777230。
旧来源 ID 包含路径、目录及各层祖先的 dev/inode/权限，因而旧登记在同一路径下失配。
原生面板启动后再次失败；捕获到 MCP 的 `REGISTRY_WORKSPACE_MISMATCH`，随后隧道退出。
五个仍存在的既有工作区都能仅通过代入原设备编号精确重现保存的完整哈希。

现在持久身份使用所在卷 UUID、inode、类型/权限及全部祖先身份；macOS 通过已打开
目录 FD 的 `fgetattrlist(ATTR_VOL_UUID)` 读取 UUID。Darwin firmlink 下 `/` 与 `/Users`
可以具有相同 dev 但不同卷 UUID，故逐个祖先读取，不能按 dev 合并。Linux 使用
文件系统 fsid；身份获取失败时拒绝访问。Linux 路径尚需独立机器的重启验收。

运行期 root/ancestor 的原始 dev/inode/mode 校验继续保留。持久绑定变化、目录替换、
符号链接和元数据版本冲突仍拒绝。schema 3 新增 stable_id，已验证旧 source_id 与
project_id 保留为逻辑 ID，避免恢复任务、执行记录和索引与登记脱离。

## 旧登记维护

旧 schema 1/2 的完整 legacy hash 与当前一致时，自动备份并升级。已经失配的登记
不遍历设备号，也不按路径直接接受。仅限用户授权的本机维护：

```sh
uv run colink registry-upgrade-identity --root <已确认工作区绝对路径> \
  --data-dir <对应live-v1/registry绝对路径> --legacy-device <经核验的原设备编号>
```

命令不属于 MCP 工具。它持有 runtime.lock，验证完整登记格式、原项目 ID 的哈希关系、
授权列表以及全部来源的原路径/inode/权限/祖先。失配或运行中拒绝修改。单个旧设备号
只适用于原来源与祖先使用同一设备编号的绑定，多卷历史失配须另行本机重新授权。
原字节备份为 `projects.pre-volume-v1.json`，权限 0600，保存使用原子替换和版本检查。

首次安装实际启动还发现恢复文件的 objects.identity 保存了旧 dev，导致
`RECOVERY_OBJECT_CHANGED`。恢复存储增加独立 `object_identities` 表（原 objects
四列和 schema 1 保留），绑定恢复目录稳定身份、文件 inode 与 mode。启动时只有
稳定身份证明成立且除 dev 外的原身份完全相同，才更新当前开机的 raw dev；运行期
读取、回收和 pending 确认继续使用原 raw 校验。存储复制同时更新复制后 raw/stable
绑定。合法 pending 未登记身份或正文写到一半的状态沿用原启动/读取语义，不升 ready。

旧 dev 已漂移而没有稳定绑定的内部文件须显式本机核验，原 inode、权限、大小与
完整 SHA-256 不匹配时拒绝并保留原数据库。命令持有 recovery.lock，备份发生在
schema/PRAGMA 写入前；备份保留主库原字节、现存 SQLite 辅助文件及一致性快照，
放在恢复目录外，事务提交前复用已有存储预算和替换检查：

```sh
uv run colink recovery-upgrade-identity --root <write-recovery-v1绝对路径> \
  --legacy-device <经核验的原设备编号> --backup-dir <独立私有备份目录>
```

源码写入与执行数据库的业务记录不随身份升级批量改写。未完成写入的 parent_identity、temp_identity、
last_version 也可能包含旧 dev，因此继续保留并阻止写入；本次不自动改写这些收据，
也不声称已经修复未完成操作在重启后的自动恢复。

恢复旧应用时需要一起评估登记格式兼容性。schema 3 不供 build 19/22 直接加载；
原登记备份用于受控恢复，先保留当前工作及新登记，不自动覆盖用户代码和外部副作用。

## 验证与安装记录

相关目录/登记/索引/存储回归 294 项、写入/恢复/执行/桌面控制回归 184 项及新增
16 项恢复身份兼容测试通过。最终完整回归 2,649 项通过，仅有既存 AnyIO 废弃警告。
Ruff 检查通过，src/tests/macos/scripts 格式检查 161 文件通过；全仓库格式检查中
既有未跟踪 db-connection-fixture/main.py 不合格式，保留用户原文件。旧测试路径误放
系统临时目录引起的目录限制失败已通过新独立 .artifacts basetemp 消除，未修改限制。

两个端到端 demo 通过。最终包为 `.artifacts/package-identity-build23-20261009-recovery-verified`。
严格签名和 79 个后端源码文件逐字节核验通过；本机已覆盖 CoLink 0.5.0b5 / build 23。
五个既有工作区、13 项登记保留原 ID、名称与授权；不存在的旧测试目录登记未改写。
10 个实际恢复文件完成绑定，内容、任务/操作/引用/状态不变。原数据库与一致性备份
保存在 CoLink-data/.code-context/identity-backups/20261009。除恢复库外原清单的 46 个
配置/数据库文件哈希保持一致。安装时实际选定的 /Users/yuyan/工作/项目 保留。

原生面板显示“已连接”；实际后端 running/ready 为 true。关闭、退出应用、重新打开
和再次连接成功，写入/开发仍默认关闭，原完成任务继续可见。最终包内 Python 通过
真实本地 MCP 的 create_file/读回/finish 和原生 terminal_start/status/output：命令
退出码 0、输出和 cwd 正确，重启默认只读，45 个工具保留。这是本机协议证据，
以上包内验收是本机协议证据。ChatGPT 网页验收随后按用户最新要求缩减为一轮，
实际结果见下节。

证据目录：`.artifacts/validation/identity-reboot-build23-20261009`，包含最终回归 XML、
安装/恢复迁移 JSON、bundled-functional-result.json、actual-stop.json 与 actual-reopen.json。
没有进行多次真实 macOS 重启；设备编号漂移通过身份注入及旧记录核验覆盖，不能声称
所有重启都会触发旧问题，也不能把退出应用验收写成系统重启验收。

## ChatGPT 网页一轮验收与清理

用户明确要求“一轮能够成功就算成功”，因此只运行一轮，没有追加网页提示重测。
通过原生 Chrome 窗口在 ChatGPT 中选择 CoLink，连接新建的独立目录
`examples/sample_project/colink_web_acceptance_20261009`；开发授权仅授予该目录。
会话为 `https://chatgpt.com/c/6ac8bbfe-1a28-83ee-a7e2-9053938c4013`。

网页 AI 实际完成 begin/create/read/edit/delete/diff/finish 写入流程；创建私有 .venv，
从 PyPI 下载并安装 colorama 0.4.6，运行 unittest（1 项通过）、compileall 与 app.py
（输出 7），均退出码 0；启动临时 HTTP 服务，实际请求返回 200，取消终端后确认
进程消失、43117 端口释放。独立核验了实际文件、依赖安装和端口状态。一轮成功，
本次成功率 1/1 = 100%；这不代表全部可选开发工具均测过，也不代表长期故障率为零。

停止连接后，按用户明确清理授权，移除上述测试目录（含 .venv、依赖缓存、源码、
字节码和日志）、仅属于该目录的 desktop/workspaces 派生目录，以及该测试的已完成
写入任务恢复材料。确认测试目录与派生目录不存在，相关进程为零，端口已释放。
选择已恢复到测试前的 `/Users/yuyan/工作/项目`。最终面板连接确认遇到 Mac 锁屏，
当前连接保持关闭；安装后的实际连接、退出重开与网页测试连接此前均已成功。

清理核验还发现既有写入恢复保留机制的副作用：`finish_write_task` 在完成新任务后
会淘汰全局其他已完成/已回退任务，`_retire_expired` 同样只保留最新一条。
本次网页测试已触发该原有行为，使之前 A 项目的一条已完成任务及 10 个修改前
恢复正文被回收。原任务数据库的原字节备份及一致性快照仍在安装时的 identity-backups
目录中，但备份不含恢复正文；本次核对的现存源码、镜像与派生目录未找到这 10 个
正文的完整副本，因此未恢复其回退能力，不能声称旧恢复材料全部保留。测试没有写入
原 A 项目源码，按用户边界未修改写入任务保留机制或其他核心功能代码。
