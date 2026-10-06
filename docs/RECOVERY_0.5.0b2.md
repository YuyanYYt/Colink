# CoLink · 0.5.0b2 首选软件恢复基线

日期：2026-10-07（Asia/Shanghai）。状态：本机固定，未推送 GitHub、未发布新安装包。
用户要求今后新功能出现重大回归时优先恢复到当前版本；旧固定锚点保留，不移动。

## 固定身份与已保存内容

- 标签：`anchor/colink-0.5.0b2-before-terminal-execution`。
- 源码提交：`8e1771f3c054724d60aafc3b3afe3fe8128c07bc`。
- 分支：`codex/colink-controlled-write`。
- 应用：`/Applications/Colink.app`；品牌 CoLink；`CoLinkVersion=0.5.0b2`；build 8。
- bundle ID：`local.codeconnect.menubar`；`LSUIElement=true`；macOS 14+ / arm64。
- 旧 `anchor/colink-0.4.3-before-live-workspace-refactor` 与
  `anchor/colink-0.4.1-before-refactor` 及发行标签原位保留。

本基线包含原文件直读、多项目与无 Git 项目发现修复、Python/Java 按需结构、默认
关闭的精准写入、创建目录/文件、单文件删除、Diff 以及未完成提交的保护。
主动用户代码回退工具已经移除，终端执行工具尚未实现。软件标签恢复与用户项目
Git 恢复是不同操作，不因保存本锚点而恢复用户项目回退按钮。

锚点取已提交软件实现，且本轮核对后端文本与安装版一致。用户尚未提交的
`docs/WRITE_CONTRACT.md` 与 `examples/sample_project/supermarket-system/` 保留原样，
**不纳入已提交软件标签或源码 bundle**；不得为保存锚点而自动提交用户样例或合同改动。
本文与后续终端提案是锚点后的维护说明，软件实现标签不因更新说明而移动。

## 离线恢复材料：不重复复制应用

源码 Git bundle 与小体积记录位于：

```text
.artifacts/anchors/colink-0.5.0b2-before-terminal-execution/
├── CoLink-0.5.0b2-source.bundle
├── RECOVERY.md
└── SHA256SUMS
```

复用现有自包含发行归档，不再新复制应用或压缩包：

```text
.artifacts/colink-install-050b2.yEyc8S/release/Colink-macos-arm64.zip
.artifacts/colink-install-050b2.yEyc8S/release/Colink-macos-arm64.dmg
```

这些文件现在同时属于本恢复锚点的保护材料。后续旧构建清理不得仅因版本旧而删除；
移动前必须更新本地引用并重新校验。ZIP 是恢复应用所需的最小二进制材料，DMG 是
现存发行格式，没有为本锚点新增第二套二进制。`.artifacts` 不进入公开源码提交。

已复核 ZIP SHA-256：
`c512efdbe368e420fba5e15fe477625b0bfcef32ae15b16ec34d50e7628a36ec`。

已复核 DMG SHA-256：
`15c0bfce2c4c6d9eba2ea29e7a0f94f8f94f80bd9a763a7262647638ed4782f3`。

当前应用完整树摘要：
`37628c6af9c79f76d2b09b6559fe498a07aaeaf948fac47713d0209a7b987061`；
1862 个文件、2 个符号链接、135673273 字节。树摘要算法为 `macos/package.py` 的
`member_summary`，包含路径、文件权限、内容摘要与链接目标，不是普通单文件 SHA。
本轮严格深度签名及 ZIP 完整性复核通过；签名仍为本机 ad-hoc，非 Developer ID 公证。

## 恢复顺序与不可恢复的范围

1. 重大软件回归先停止故障连接与相关写入/执行任务，保存当前源码、未提交改动和
   必要日志，不直接 hard reset、强制 checkout、覆盖标签或清空数据库。
2. 从固定标签建立**独立恢复分支/checkout**。当前工作区脏时不强制切换；优先独立
   checkout。确认标签目标仍为上述提交，源码 bundle 可用 `git bundle verify` 验证。
3. 需要恢复应用时先核对归档 SHA；退出应用并确认自有进程停止后，再在确定路径
   安全替换安装版，不覆盖运行中的后端或隧道。安装路径/身份不改变。
4. 检查后续版本对项目登记、索引、任务状态及数据库的格式变化。0.5.0b2 支持项目
   登记 schema 1/2；不代表能读未来任意 schema。没有兼容/迁移路径时先报告，
   不用删除运行数据或重置用户设置来绕过问题。
5. 原目录选择、私有连接、凭据、授权范围、当前/复用数据库、锁文件及未完成写入
   记录保留；连接与权限遵循本版本启动语义，不因恢复自动开启或扩大。
6. 本机恢复成功与网页工具恢复分别验证；未来工具清单变化时需要刷新原网页连接，
   不另建更广权限的连接。本轮未重做真实网页验收。

此标签/bundle/安装包只保存 CoLink 软件，不保存运行数据库、凭据或用户项目未提交
文件。它们不能撤销用户项目写入、系统依赖安装、数据库迁移、GitHub 已推送记录或
任意外部副作用；重要用户内容仍应由其项目 Git / 经授权的数据保护流程管理。

保存锚点不等于零 bug、正式稳定版或异机灾备。本地记录的历史 1871 项源码回归与
包内 stdio 验收不替代新的真实网页验收。未来恢复前仍应重新核对当前事实和权限。
