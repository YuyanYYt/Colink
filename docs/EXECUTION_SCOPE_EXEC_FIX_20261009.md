# macOS 终端进程监控修复 · build 22

## 复现与根因

实际 CoLink 终端作业 `job-03d34917c58ae1eb2310c3bf475df99c` 启动过 3 个
进程，随后返回 `process_observation_failed`、
`EXECUTION_SCOPE_IDENTITY_CHANGED`、退出码 143；清理记录表明整个进程组已停止。
该结果不表示用户的开发授权不足。

在本机独立子进程中，`exec` 前后 PID 与 `p_uniqueid` 相同，`p_idversion` 增加 1。
原来的资源组扫描、资源采样和发信号路径都把完整三元组变化视作进程替换，
因此普通命令启动链也可能误触发失败。Apple XNU 的
[进程身份校验实现](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_proc.c)
同样将“允许 exec 时按 PID 与 unique ID 识别同一进程”和“审计令牌按 pidversion
核对当前映像”区分处理。

现在扫描遇到同一进程执行 `exec` 会重读 UID、状态和版本；资源组或 unique ID
改变仍报错。旧映像的内存状态不计入当前采样；CPU 累计值按不变的 unique ID 记账，
避免 `exec` 后重复累计。向进程发送信号前重新核对 UID、资源组和当前版本，
最终仍由内核审计令牌原子校验目标版本。反复变化、身份不明或进程组未清空时
继续按失败处理。

## 验证

- 修复前两项确定性回归失败；修复后相关进程监管 **72 项通过**。
- 使用独立 `.artifacts` 临时目录完成全量回归：**2,611 项通过**，1 项现有的
  Starlette/AnyIO 弃用警告。Ruff 检查及 154 个文件格式检查通过；HTTP 与本机
  stdio demo 均包含在全量回归中。
- 本机测试包含两个真实执行 `exec` 的服务子进程，保持运行、被资源组监控，
  取消后全部清空。安装版的内置 Python 另跑同类隔离探针：观察到 3 个进程，
  最终 `cancelled`、`cleanup_verified=true`、`observation_error_code=null`；
  结果保存在 `.artifacts/validation/process-scope-build22-20261009/installed-native-fixture/result.json`。
- 未对项目 A 运行 Python/Maven 服务，也未复验其 HTTP 502；这需要项目自身的
  服务与网页工具调用。ChatGPT 的工具安全检查属于独立控制层，CoLink 代码更新
  不会改变其审批结果。长期服务应分别以保持前台运行的终端作业启动。

## 安装、签名与清理

`/Applications/Colink.app` 已更新为 CoLink `0.5.0b5` / build `22`。
新包与安装版均通过严格签名；正常启动前后的安装版文件树与最终打包报告相同，
共 2,606 个文件。应用保留了项目 A 的选择；重启连接后原生面板显示“已连接”，
实际 MCP 可达，写入和开发授权均关闭。首选恢复锚点仍是
`anchor/colink-0.5.0b5-build19-verified`。

安装版隔离探针曾让内置 Python 的子进程生成字节码缓存，使签名暂时失效。
这个探针副本保留在
`.artifacts/validation/process-scope-build22-20261009/Colink.installed-probe-pyc.app`；
随后从已签名的最终包重新安装，并验证正常连接前后签名和文件树均有效。
被替换的 build 21 应用已移至
`~/.Trash/Colink.previous-build21-20261009-process-scope.app`，可恢复。

用户要求先清理旧测试再继续回归。本轮按清单删除 14 个已结束的 pytest 生成目录，
合计约 3,261 MiB；清单保存在
`.artifacts/validation/process-scope-build22-20261009/old-pytest-cleanup.json`。
新一轮完整回归目录、两份 build 22 打包产物、基准数据、源码、凭据及当前数据库保留。
