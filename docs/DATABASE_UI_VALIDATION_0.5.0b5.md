# CoLink 0.5.0b5 数据库、模式与存储位置验收

日期：2026-10-08。数据库后端已冻结；用户后续要求聚焦 UI，最终视觉验收在完成后补齐。

## 实现范围

- 只读 / 写入 / 开发三个模式按钮；开发授权在同一窗口选择项目和端口。
- 数据库窗口使用圆角透明材质卡片，与主面板保持一致；显示项目、具体连接候选和真实状态。
  项目级手动连接表单移除；开发环境采用红/绿/灰灯卡片。存储位置收进齿轮设置。
- 仅在已允许项目内静态解析配置。多个目标或来源冲突需要选择；调用前重新读取。
- 第一次授权绑定实例、数据库、账号、认证与安全参数，保存在本机；同一目标可跨项目复用。
  数据库授权保留到明确撤销或目标/认证参数变化；写入与运行授权仍在断开、退出后关闭。
- 固定数据库读取只提供表清单、结构和最多 100 行预览，不接收任意 SQL。
- 开发执行绑定 `database_target_id`。新项目通过 `database_environment` 和 `database_prepare`
  准备服务与具体库，本机批准执行固定建库流程；项目作业不获得服务管理员账号。
- 主面板不显示存储栏或路径；齿轮设置中的存储入口默认折叠并支持自定义目录。
  新安装默认 `~/CoLink-data`；迁移只复制应用持久状态，
  校验副本、重绑内部状态引用，保留原始项目路径与原目录。

## 已完成的真实数据库验证

以下首轮管理员账号路径验证发生在用户追加“不能操作其他库”的要求之前。
最终版本需要追加有限账号、新项目引导和原生跨库拒绝验证，不用首轮结果代替这些证据。

验证使用本工作目录内的独立测试实例。未连接、初始化或修改现有业务实例。

| 实例 | 地址 | 账号 | 验证 |
| --- | --- | --- | --- |
| PostgreSQL 18 | 127.0.0.1:54487 | postgres，测试实例 trust | 实际登录与目标/账号匹配、表结构、限量预览、授权拒绝/跨项目复用/重启保留/撤销 |
| MySQL | 127.0.0.1:54488 | root，测试实例无密码 | 实际登录与目标/账号匹配、表清单、结构、限量预览 |

原生执行器已对 PostgreSQL 普通库 `store_fixture` 完成建表、插入和查询，退出码为 0，
进程监督报告 `cleanup_verified=true`，固定读取读回结果一致。该结果验证本次执行路径；
不把现有 `isolation_complete=false` 状态改写为完整隔离证明。

PostgreSQL 与 MySQL 分别完成批准未存在的具体库 → 受控创建 → 受控删除 → 授权撤销 →
未批准重建计划拒绝 → 重新批准 → 创建并真实登录。共六个原生作业均退出 0。

测试报告与脚本位于 `.artifacts/validation/database-ui-0.5.0b5-20261008/`，包括
`real-postgres-report.json`、`real-mysql-report.json`、`native-postgres-report.json`
及 `database-lifecycle-report.json`。生成物保留，测试服务器在验收结束后停止。

## 客户端与授权边界

数据库 UI 资源和候选选择已实现。候选选择不授予数据库权限；第一次授权通过本机
认证控制通道完成。不能把模型传入 `approved=true` 当作用户点击的证明。
ChatGPT 自己的工具确认仍由客户端控制，CoLink 的本机许可不替代它。

用户随后允许在聊天凭据表单尚未实际支持时，采用本机一次连接服务并长期复用的入口。
服务连接不批准所有数据库；每个项目库分别授权。普通 MCP elicitation 不用于收集密码，
依据 [OpenAI MCP server 文档](https://developers.openai.com/plugins/build/mcp-server)。
聊天 UI 与 app-only 工具及隐藏元数据有官方支持，但本私有 Tunnel 的凭据表单未作实际验收，
见 [OpenAI UI 参考](https://developers.openai.com/plugins/reference)。

新增的原生 Keychain 读写完成真实保存/读回，10 项相关测试通过。
PostgreSQL 目标代理 32 项真实 TCP 协议测试通过；在独立 PostgreSQL 18 实例中，
psql 实际连接目标库成功，其他库启动和 `\\connect` 切换被拒绝，报告为
`native-keychain-report.json` 与 `real-pg-target-proxy-report.json`。

最终执行路径使用新建有限账号、每目标代理和原服务端口禁止规则。
MySQL 精确库级授权与 PostgreSQL 非管理员角色限制 SQL 权限；代理另外校验
认证数据库/账号并拒绝换库/换用户。服务管理员凭据仅进入固定本机准备流程。
当前本机目标代理不支持 TLS，相关目标拒绝执行，不自动降级。Qdrant 没有执行后端。

## 有限账号与原生范围实际验证

独立 PostgreSQL 54487 与 MySQL 54488 完成固定本机准备 → 新库及有限账号 →
专属代理真实登录 → 建表、插入与预览。报告 `strict-bootstrap-report-21bcf261e5b9.json`。

`native-scope-report-76b38dbacc8b.json` 包含四个真实沙盒作业：两引擎各一次 CRUD 和
一次负向范围检查。原服务端口直连、异库启动、换成 postgres/root，以及服务器跨库
权限操作均被拒绝；四个作业退出 0，`cleanup_verified=true`。
重启两个 Runtime 后，具体库授权保留，实际认证通过。

首轮一个短作业遇到 `EXECUTION_SCOPE_IDENTITY_CHANGED`，原始状态与产物保留。
全新状态复测通过，未修改或放宽进程观察器；不把该复测称为通用观察器问题已经消失。
同样不将执行报告中的 `isolation_complete=false` 改写为完整隔离证明。

现有 PostgreSQL 对象 ownership 保持原状，ALTER/DROP 原有非有限账号所有对象可能被
服务器拒绝；新库由有限账号拥有，新库 DDL 已实际验证。危险扩展、外部连接和 definer
对象的旧库会拒绝批准。TLS 目标代理尚未支持，相关目标不降级。

## 空目录 Spring Boot 实际验证

在 `examples/sample_project/spring-bootstrap-967d44de1f` 的初始空目录，完成
真实 MCP 环境查询、服务识别、建库准备、受控源码/配置写入、本机具体库批准、
Maven 构建和 Spring Boot 3.5.6 程序运行。共 124 次 MCP 调用（包含状态轮询）。
Spring JdbcTemplate 校验实际库名与随机有限账号，再完成建表、插入、查询；
固定读取接口读回相同数据。构建和运行均退出 0。

该证据验证 Spring Boot JDBC 应用连库；程序为非 Web 验收程序，不代表 Tomcat HTTP 接口验收。
报告 `spring-bootstrap-report-967d44de1f.json` 保留；测试库与项目文件保留。

## 本机迁移和安装

用户选定运行数据目录：`~/工作/CoLink-data`。
旧目录：`~/Library/Application Support/Colink`。

只读预检发现 93 个应用状态文件、67,768,971 字节，其中包含 SQLite 临时 WAL/SHM。
首次迁移因 SQLite 最后一个连接关闭时辅助文件消失而失败，安装流程恢复原应用；
部分复制另存为 `CoLink-data-migration-incomplete-20261008`，没有清空源数据。

修复后使用 SQLite 只读事务快照和备份 API 包含已提交 WAL，校验逻辑摘要与
`quick_check`，不将临时 WAL/SHM 作为持久文件复制。实际迁移成功：81 个持久文件，
67,572,363 字节。连接配置文件 SHA-256 和目录选择原文一致，原始项目路径保持原样，
原 Application Support 目录保留。迁移报告见 `installation-report.json`。

## 回归与提交

数据库后端冻结版本全量测试 2547 passed（167.77 秒，1 项依赖废弃提示），Ruff 检查和格式检查通过。
HTTP MCP demo 与持续 stdio demo 通过；这些不代表 ChatGPT 网页 UI 实际验收。
原生凭据链路另经合成凭据验证：Swift 私有 stdin → 捆绑 Python store → 同二进制无交互 read，
内部摘要相符。此前跨程序 ACL 方案实测失败，未保留在生产实现。
最终 UI 相关测试、安装版本与真实点击记录待补齐。
公开下载仍为 0.5.0b2，本轮不推送或发布 GitHub。
