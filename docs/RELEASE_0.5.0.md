# CoLink 0.5.0 正式版 · build 43

## 本版更新

- Live 架构图支持文件和边独立分页。使用相同查询范围和返回的 `next_cursor` 可续取，大型循环组使用稳定标识。
- Python 文件导入和成员解析分别保留证据。Vue 普通脚本和 `script setup` 支持 JS/TS 静态导入、重导出、字面量懒加载和可确认的 Vite 别名。文件图保留原始行列及解析受限的原因。
- Java 和 Python 的调用证据记录分支、提前返回和源码位置。静态调用路径可报告已识别的条件矛盾，运行时可达性继续标为未知。组合分析在字符预算紧张时保留覆盖计数和限制摘要。
- Live 默认档支持最多 25 万文件、200 万结构事实和每项目 2 GiB 数据库，CLI 可选择更高的 `extreme` 档。详见 [容量配置与验证规模](https://github.com/YuyanYYt/Colink/blob/v0.5.0/docs/LARGE_PROJECT_CAPACITY_20261029.md)。

## 下载与升级

- `Colink-macos-arm64.dmg` 为拖放安装包，`Colink-macos-arm64.zip` 为备用归档。
- `colink-code-context-skill.zip` 提供可选的调用指导。应用安装、隧道和项目授权由用户在本机配置。
- 下载后可在同一目录运行 `shasum -a 256 -c SHA256SUMS` 校验归档。

应用面向 Apple Silicon，最低要求 macOS 14，包含 Python、锁定依赖和官方隧道客户端。安装包采用 ad-hoc 签名，尚未进行 Apple Developer ID 签名或公证。安装方式见 [安装指南](https://github.com/YuyanYYt/Colink/blob/v0.5.0/docs/INSTALL.md)。

升级前请退出旧应用。新版按需重建 Live 派生索引，原项目目录、连接设置和写入恢复记录由本机保留。历史安装包列在 [GitHub Releases](https://github.com/YuyanYYt/Colink/releases)。
