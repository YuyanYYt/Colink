# MCP 开发权限状态修复 · build 21

## 实际复现

2026-10-08 用户截图显示 CoLink 选中开发档，而网页显示命令通道未开启、闸门 closed。
实际界面与本机 desktop-status 确认开发授权已开启；运行版 0.5.0b5 / build 20：

| 状态 | 本机运行时 | 网页 MCP |
| --- | --- | --- |
| execution_available | true | false |
| execution_enabled | true | false |
| execution_gate | passed | closed |
| execution_projects | 当前项目 | 空列表 |

原生终端实际 MCP 启动 `/bin/echo COLINK_TERMINAL_READ_PROBE_OK`，读取输出及终态成功，
退出码 0，cleanup_verified=true。该探针只输出测试文字，不修改用户项目文件。

## 根因与修改

`LiveQueries.mcp_status()` 只合并 WriteCoordinator 状态；CLI 的实际 MCP 状态来源是此
方法，没有传入 ExecutionCoordinator 状态。`connection_status` 因字段缺失返回默认
false/closed，使网页版 AI 误判本地开发权限。

live 状态改为使用 attached WorkspaceRuntime.status()；独立 LiveQueries 继续使用
原来的写入状态或空状态。运行时控制失效撤权、默认关闭与平台保护沿用现有逻辑。
MCP 对外继续只返回诊断白名单，不返回本机控制令牌、任务请求回执或私有目录。
执行项目列表与执行 enabled 按连接的实际可见项目过滤，避免其他项目授权误报。

execution_available 表示当前平台与运行时可用，execution_enabled 和 execution_projects
表示本机确实授权的项目。execution_gate=passed 单独不表示用户开发授权。

## 验证

- 修复前新增回归复现 5 项失败、1 项通过；修复后 6 项全部通过。
- 相关 205 项通过，包括原文件直读、MCP/stdio、工作区权限/重启、桌面授权转发、
  执行协调器和数据库/终端工具定义。原有 Starlette/anyio 弃用警告保留。
- Ruff 检查通过；154 个文件格式检查通过。
- fixture 中模拟平台可用性，仅证实状态与授权协议，不代替真实 macOS 进程监督验收。
- 首选恢复锚点继续为 anchor/colink-0.5.0b5-build19-verified，不移动或重建。

## 安装验收

安装版验收完成后补入实际版本、签名、MCP 与配置保护结果。重新启动仍默认只读；
本机开发授权不会因修复或安装而自动开启。用户项目、配置、凭据及当前数据库保留。
