# CoLink build 25 启动状态管道修复与验收

## 故障与原因

用户反馈 build 24 无法启动 MCP。实际菜单栏点击启动后进入连接中，随后约 12 秒
退出。监督记录为正常停止 exit_code=0；后台独立探针能够立即返回小状态帧，因而
不能只根据界面概括为网络故障。

提取 build 24 的实际原生 DesktopStatusStream 后接到真实监督进程：946–2912 字节
状态直到停止、管道 EOF 后才交付。Foundation read(upToCount: 65536) 在持续管道
等待更多数据，触发已有状态超时并整组停止。build 24 此前的安装验收只证明关闭
界面与无子进程，漏掉了启动场景；全量源码回归不能替代这一场景。

## 修复范围

原生持续状态读取改为单次有界 Darwin.read；分段数据继续交给原解码器。EINTR、
EAGAIN、EWOULDBLOCK 保留后续读取机会；EOF 与其他读取错误仍沿原故障关闭。
保持 4 KiB 请求/64 KiB 响应预算、状态世代和序号验证、12 秒超时及完整进程组退出。
未修改源码工具、终端、数据库、开发授权、既有时间预算或 300 秒节能窗口。

新增真实活管道回归以 /bin/cat 保持输出管道未关闭，要求两个小帧（其中一个分段
写入）在进程仍运行时交付。71 项相关测试通过；Ruff、格式检查、Swift 全源类型
检查和打包通过。此次没有再次扩大为无必要的全量回归。

## 安装和实际验收环境

- 安装路径 `/Applications/Colink.app`；CoLinkVersion 0.5.0b5 / build 25，
  bundle ID local.codeconnect.menubar。严格深层签名与构建源 2609 份文件一致。
- 为避免测试任务替换原来的全局恢复材料，网页测试使用新的独立来源目录，以及
  完全独立的 CoLink 运行数据、登记和恢复库。复用原连接身份，不新建公网端口。
- 私有验收应用与安装版的原生执行文件在去掉签名及签名尺寸字段后字节一致；后端
  由安装版复制。仅调整临时 bundle ID 与 runtime 配置，隔离应用偏好和数据。
- 测试准备初次复制的 profile 引用旧 Python 路径，重新以私有应用生成；另一准备
  路径过长使 AF_UNIX 控制端点无法建立，缩短临时数据路径后消除。两者是验收环境
  配置问题，没有据此修改产品或弱化目录/端点校验。
- 实际测试界面显示已连接、开发模式；网页已经读取独立测试目录的种子文件。

## 一轮真实网页验收结果

| 场景 | 网页结果与本机核对 |
| --- | --- |
| 读取 | list_projects / repo_overview / read_file 返回独立项目与种子正文 READ-OK-CAB81F |
| 写入与删除 | 同一任务 create_file / apply_edit / read_file 成功，最终 WRITE-UPDATED-CAB81F；delete_file 删除临时文件；finish_write_task completed |
| 终端与依赖 | terminal_* 返回 TERMINAL-OK-CAB81F；项目内 .venv 创建、six==1.17.0 下载安装和导入成功；5 个终端作业退出码全部为 0 |
| 数据库 | .venv Python 的 sqlite3 建表、插入两行、查询、更新第 1 行、删除第 2 行、提交并重新打开；仅剩 (1, DB-UPDATED-CAB81F) |

本机另行核对种子文件、写入正文、删除对象不存在、虚拟环境隔离与 six 导入位置，
以 SQLite mode=ro 再查最终持久化内容，均与网页结果一致。网页完整完成 4/4 核心
阶段，单轮验收 1/1 成功；这不等于长期成功率或全部数据库类型已测试。本轮数据库
为 SQLite，未接触用户真实数据库、其他项目或全局依赖。

关闭按钮后的监督记录为 stopped / exit_code=0；6 个原生测试 scope 均已从 launchd
卸载（其中含环境探测，网页终端作业为 5 个）。测试应用退出后删除新建测试目录、
其文件/.venv/SQLite、两份临时运行数据、临时应用与偏好、此次 pytest 目录和诊断
探针。保留不含凭据的修复/安装/验收/清理收据与最新 build 25 DMG/ZIP。

替换下来的 build 24 应用、旧打包目录（包含旧 DMG/ZIP）及 4 份可证明属于 builds
23/24 的已完成 pytest 派生目录，移入 `~/.Trash/CoLink-build24-retired-after-build25-20261010`，
可恢复。未删除 `.code-context` 原始持久数据、源码、用户未提交改动、配置或锁文件。
原正常数据目录的 426 份文件 SHA-256 在安装与测试清理后保持一致。

正常 `/Applications/Colink.app` 已重新打开，实际界面显示“已关闭”、原 LangGraph
选择和默认只读；未为最终恢复再次连接原项目。安装版仍通过严格深层签名检查。
原始诊断/安装/网页验收/清理收据位于
`.artifacts/validation/startup-regression-build24-20261010/`；最新安装包位于
`.artifacts/package-startup-fix-build25-20261010/`。未发布 GitHub。
