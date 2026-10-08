# 网页开发与数据库命令 · build 17

## 模式

| 模式 | 文件操作 | 数据库 | 终端 |
| --- | --- | --- | --- |
| 只读 | 读取已启用项目 | 固定库表/字段/数据读取 | 不允许通用命令 |
| 写入 | 受控源码编辑 | 与只读档相同 | 不允许通用命令 |
| 开发 | 源码工具与本机命令 | CLI/驱动具备账号允许的查询、增删改、建库、删库、建表、迁移能力 | 当前 macOS 用户权限 |

没有独立数据库连接页面、逐库授权、数据库准备工具或网页 MCP 卡片。
旧连接数据、凭据、数据库和恢复记录保留；新终端不依赖它们。

## 开发档

1. 本机为具体项目开启开发档；网页调用 `execution_environment(project_id)` 查看已有工具。
2. 读取当前项目配置。账号密码在本机配置或进程环境中使用，避免打印和放进 argv。
3. 调用 `terminal_start(project_id, request_id, command, ...)`。command 是 argv；
   `['shell', script]` 使用本机 zsh。初始目录是真实项目目录，可用相对 cwd 指定子目录。
4. 返回作业 ID 后查看 `terminal_status`，running 时用 `terminal_input` 继续发送命令。
   在 SQL CLI 中发送命令时加换行。PTY 支持密码提示，默认关闭输入回显；应用仍可能自行打印。
5. `terminal_output` 按游标读取输出；`terminal_list` 在网页刷新后找回会话。
   `terminal_cancel` 停止会话，随后确认状态。撤权/本机断开会请求停止已拥有的进程。

`service=true` 适合数据库交互和长服务，空闲 20 分钟停止；持续读取状态/输出续租。
`service=false` 适合有限命令，最多 300 秒。可同时运行两个持续会话和一个有限命令。
输入单次最多 16 KiB，输出单次最多 64 KiB；输出缓冲和保留时间有限。
启动和输入使用稳定 request_id。投递成功不等于 SQL 成功；不确定结果不应换 ID 盲目重试。
运行时重启不自动重放命令。

数据库命令没有 CoLink 逐库限制，上限来自所用账号。MySQL、PostgreSQL、Redis、SQLite，
以及 Python/Node 驱动都可通过终端使用，前提是本机实际安装并具备可用配置。
新 Spring Boot 项目可直接通过 SQL 客户端建库、写 datasource 配置，再运行 Maven/迁移。
CoLink 不自动安装数据库，也不能绕过数据库认证。

项目选择只决定启动目录和授权入口；开发终端可以访问当前用户有权限的其他文件与服务。
终端直接写入不经过源码工具的恢复记录；停止进程不会撤销已执行的 SQL 或文件修改。

## 只读档

1. `terminal_read_targets(project_id)` 返回脱敏配置目标及支持的读取命令。
2. `terminal_read(project_id, client, action, target, ...)` 执行固定命令：
   - mysql/psql：`list_databases`、`list_tables`、`describe`、`preview`，预览最多 100 行。
   - redis-cli：`scan`、`get`、`type`、`ttl`、`hgetall`、`lrange`、`scard`、`zrange`。
   - sqlite3：`list_tables`、`describe`、`preview`；target 是项目内现有数据库文件的相对路径。
3. SQL/Redis 使用本机项目配置中的凭据，无连接页面或保存连接步骤。
   未识别、未解析或冲突配置需先在本机修正；识别到配置不代表认证成功。

只读入口不接收任意 SQL、脚本、客户端参数或写命令；复杂 SQL 和变更使用开发档。
SQL 固定读取加只读事务，Redis 固定命令白名单，SQLite 使用只读连接与 authorizer。
只读限制约束 CoLink 发出的命令，不代表对数据库服务端实现及其外部副作用的审计。

## 可选隔离构建

原 `execution_plan/rehearse/start` 仍用于需要隔离输入、临时磁盘和受控写回的构建任务。
它的 Unix socket 服务适配、包源/端口限制和 cache venv 参数不适用于 `terminal_*`。
普通 Spring Boot TCP 服务可通过开发终端直接运行，无需改成 Unix socket。

## 验收

本轮仅执行静态检查、编译、打包与安装核对；用户负责实际界面、网页工具刷新、
PTY 输入、真实连接/CRUD、只读拒绝与撤权停止验收。不能以构建成功代替运行证明。
