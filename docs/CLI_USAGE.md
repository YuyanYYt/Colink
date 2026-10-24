# CoLink · 命令行与 MCP 客户端

此文保留高级运行方式；普通 macOS 用户可直接使用 [应用安装包](INSTALL.md)。
所有命令在仓库目录运行，先执行 `uv sync --locked`。`colink` 是当前入口，旧
`code-context` 别名继续兼容；内部模块、配置变量和应用路径不随品牌文字修改。

## 推荐：原文件按需直读

单项目只读 stdio 入口：

```sh
uv run colink live --root examples/sample_project --project sample \
  --data-dir .code-context/live-sample
```

由支持 stdio 的 MCP 客户端启动，不是网页可访问的 HTTP 地址。客户端配置示例：

```json
{
  "mcpServers": {
    "colink": {
      "command": "/absolute/path/to/Colink/.venv/bin/colink",
      "args": [
        "live",
        "--root", "/absolute/path/to/Colink/examples/sample_project",
        "--project", "sample",
        "--data-dir", "/absolute/path/to/Colink/.code-context/live-sample"
      ]
    }
  }
}
```

将示例绝对路径换成真实安装位置。只有你选定的目录可读；不需要 HTTP 令牌，也不
自动修改客户端配置或创建 ChatGPT 连接。修改用户项目时必须另有明确授权。

### 多项目工作区

`workspace-init` 在本机登记选定目录并发现候选项目；`workspace` 只运行已确认
范围。GUI 用户推荐用菜单栏“项目管理”完成确认，不通过网页扩展授权。

```sh
uv run colink workspace-init --root examples/sample_project \
  --data-dir .code-context/workspace-sample
uv run colink workspace --root examples/sample_project --project sample \
  --data-dir .code-context/workspace-sample
```

命令中两次的根目录和数据目录必须一致。具体授权与受控写入步骤见
[LIVE_USAGE.md](LIVE_USAGE.md)。命令本身不授予写权限。

### Live 读取与分析工具

当前 build 33 用 `read_code` 读取已知文件、准确符号、文本/符号搜索及最多五个
显式范围；`analyze_code` 返回结构、数据传播和任务 Diff，非 diff 分析可附带
selector 的实际源码。精确文件读取不要求先调用分析或完成索引。

Live 只读工具 9 个，带写协调器时 19 个，完整工作区含开发工具 40 个；发现即
实际注册。八个被替代的旧名字不再可调用，`live` / `workspace` 不再接受
`--legacy-analysis-tools`，`build_mcp` 也没有恢复旧注册的开关。更新客户端缓存
后使用新 schema，具体请求与预算见 [入口说明](CODE_ANALYSIS_ENTRYPOINT.md)。
`write_batch` 是一个额外的批量写入口，复用当前写任务与本机授权；最多二十笔
显式编辑/创建先整体预检，再逐项执行。不提供批量删除或移动，也不承诺多文件
原子事务，详见 [使用指南](LIVE_USAGE.md)。

## 旧镜像兼容：持续 stdio

仍需使用旧模式的客户端可运行：

```sh
uv run colink local --root examples/sample_project --project sample \
  --data-dir .code-context/local-sample
```

它监听指定目录、更新本地只读镜像，不开放 HTTP 端口。每个数据目录绑定一个
root/project，监听异常时拒绝读取，避免把旧镜像冒充在线状态。只读查看状态：

```sh
uv run colink local-status --data-dir .code-context/local-sample
```

只采集一次可使用 `snapshot`，再用 `mcp` 读取：

```sh
uv run colink snapshot --root examples/sample_project --project sample
uv run colink mcp --data-dir /absolute/path/to/Colink/.code-context
```

旧模式只保留当前与前一份源码状态，不按编辑次数无限保留历史；读取标识过期明确
拒绝，不悄悄替换为最新代码。此模式的 `get_diff` 比较两份镜像，直读模式的 Diff
则通过 `analyze_code` 的 `diff` 模式使用受控写任务起点，二者不能混用历史含义。
Mirror 仍保留原十五工具，包括原读取、搜索和图工具；它不提供 Live 双入口。

## 旧镜像兼容：HTTP 同步与查询

服务端使用两个不同的令牌，各至少 32 个非空白 ASCII 字符：读取客户端使用
`CODE_CONTEXT_READ_TOKEN`，同步客户端使用 `CODE_CONTEXT_SYNC_TOKEN`。
不要把值写到命令参数、源码或日志。以下是 zsh 的静默输入方式：

```sh
read -rs 'CODE_CONTEXT_READ_TOKEN?输入读取令牌：'
export CODE_CONTEXT_READ_TOKEN
read -rs 'CODE_CONTEXT_SYNC_TOKEN?输入同步令牌：'
export CODE_CONTEXT_SYNC_TOKEN
uv run colink serve --host 127.0.0.1 --port 43116
```

上传终端设置同一个同步令牌，先预览再启动监听：

```sh
uv run colink scan --root examples/sample_project
uv run colink watch --root examples/sample_project --project live-sample \
  --server http://127.0.0.1:43116
```

单次上传和本地队列检查分别为：

```sh
uv run colink sync --root examples/sample_project --project live-sample \
  --server http://127.0.0.1:43116
uv run colink status --root examples/sample_project --project live-sample \
  --server http://127.0.0.1:43116
```

`status` 不需要令牌、不占上传锁。健康检查为 `http://127.0.0.1:43116/health`，
MCP 端点为 `http://127.0.0.1:43116/mcp`；客户端发送
`Authorization: Bearer <读取令牌>`。本示例只监听回环地址，不作为公网部署方案。
HTTP 同步接口和 MCP 查询分开，旧入口始终只读。每个远程项目只能有一个上传来源。

## 开发演示和详细说明

```sh
uv run colink demo
uv run colink demo-local
```

演示只使用自建样例，不需要模型密钥或启动远程连接；结果保存在新的
`.artifacts/` 子目录，不自动清理旧产物。不要把演示通过当作真实网页连接已经成功。

- [原文件直读与任务写入](LIVE_USAGE.md)：新模式的上下文、权限、Diff 与容量限制。
- [代码结构查询](CODE_INTELLIGENCE.md)：Python/Java 工具与静态分析限制。
- [本机私有隧道](LOCAL_HOST.md)：旧入口的源码连接配置。
- [架构与协议](ARCHITECTURE.md)、[安全边界](../SECURITY.md)、[贡献指南](../CONTRIBUTING.md)。

`.code-context` 是持久化状态，不是普通缓存；不要在升级或排错时直接删除。
