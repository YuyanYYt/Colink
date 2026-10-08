# 数据库 UI 与本机 Redis · build 16

日期：2026-10-08。参考本轮用户提供的数据库连接设计图；能力以现有代码和用户追加的本机 Redis 配置要求为准。

## 变更

- 数据库页采用玻璃材质、青绿色类型卡片、对齐的配置字段和固定底部反馈/操作区。
  类型为 MySQL、PostgreSQL、Redis；不显示未实现的 SQLite/MongoDB 管理功能。
- 项目配置继续自动识别；原生服务连接表单用于一次性保存本机服务凭据，包含 Redis 索引、
  无密码选项、密码显示切换和折叠 TLS 设置。数据库/项目/服务选择控件的整个字段可点击。
- 已有 database_environment → database_prepare → 本机创建并授权 → 项目开发授权流程
  补齐说明、状态返回和桌面入口。主页提示待创建目标，数据库页更新远端准备结果。
- 数据库授权与当前 project_id 的开发权限单独显示。注册子项目继续使用自身的权限，
  本轮没有改变父子目录隔离或从网页直接授予权限。
- Redis 使用固定 AUTH/SELECT/PING 检查并保存本机钥匙串凭据。支持 .env、Spring properties/yaml
  的本机 Redis 配置，未解析变量、远程地址与同目标凭据冲突不作为可用连接。
  Redis 仅提供连接检查，不开放数据命令，不将索引号视为 SQL 库授权。
- 补充项目身份检查，丢弃切换项目后返回的旧数据库结果。

## 证据与限制

- 本机只读盘点发现 redis-cli、redis-server，6379 在回环地址监听；没有执行 Redis 认证或 PING。
- Swift 类型检查及最终优化编译通过；所改 Python 的 Ruff/格式检查、网页卡片 JavaScript 语法检查通过。
- 自包含应用、ZIP/DMG 构建及严格签名检查通过；包内 80 个后端源码/资源文件与工作区一致。
- `/Applications/Colink.app` 已安装 0.5.0b5 / build 16；安装脚本核对原配置、项目目录选择和
  存储位置没有变化。安装过程正常退出旧客户端，不会自动重新开启写入或执行权限。
- 用户明确指定实际测试由其本人执行。本轮未执行 pytest、真实数据库连接、建库、CRUD、
  网页 MCP 调用或新界面交互验收。新增的 Redis 合成回归用例保存在 tests/test_redis_connection.py，尚未执行。
- 参考图与运行界面的视觉对比也留待用户验收，不能由编译成功推断视觉或功能已通过。
- 仓库原有 docs/WRITE_CONTRACT.md 的空白格式提示保留，本轮所改文件的 diff 空白检查通过。

final result: pending user validation

安装记录：`.artifacts/validation/database-design-build16/installation-build16-report.json`。
旧版备份和本轮构建产物保留，未清理。
