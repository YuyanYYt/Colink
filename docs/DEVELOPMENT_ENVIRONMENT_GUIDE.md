# 开发前先确认这台电脑能做什么

网页是 AI 开发会话入口。开始一个项目或更换项目后，先调用 `execution_environment(project_id)`，
再调用 `database_status(project_id)`。环境检查只运行固定的版本查询，不读取用户的语言启动配置、
数据库密码或项目业务模块，也不会安装软件、启动数据库或新建数据库实例。

结果里的 `available` 表示该安装位置通过了本次版本检查。`present=true` 而
`probe_state=timeout/probe_failed` 表示发现了工具但检查尚未成功，不等于工具不存在。
客户端、服务端二进制、已运行的服务、数据库连接、扩展、migration 和业务功能是不同证据。

## 1. 选择项目实际使用的运行时

| 项目 | 先看什么 | 开发过程中保持一致 |
| --- | --- | --- |
| Python | Python 路径/版本、venv、ensurepip；所需包是否有安装元数据 | 创建环境、安装依赖、测试、后端服务使用同一个解释器 |
| npm | Node 与 npm 的实际路径/版本 | 包管理器使用已绑定的 Node；保留原依赖锁文件 |
| Maven | JDK 与 Maven 的实际路径/版本 | Maven 使用本次绑定的 JDK；核对项目要求的 Java 版本 |
| MySQL | mysql 客户端、mysqld 二进制及项目连接配置 | 复用已授权实例，用项目专属数据库和角色 |
| PostgreSQL/向量 | psql、postgres、pgvector 扩展和真实 SQL 证据 | 客户端存在之后仍要检查扩展与检索结果 |

`runtime_mismatches` 会提示项目 Python 与 CoLink 自身 Python 不同，或本机已安装工具尚未绑定到
受控执行器。CoLink 能启动，不代表项目依赖已经安装；包元数据存在，也不代表该模块已成功导入。
遇到缺失项，先选择已安装的兼容工具或由用户明确安装，重新检查后再制定执行计划。
macOS 的系统 Java/Python 安装启动器不会被当作已安装运行时，也不会触发安装提示。

## 2. 为 Python 建立私有环境

在本机为当前项目开启受控终端后，将虚拟环境建在 CoLink 拥有的任务盘或项目缓存盘。
保留源码目录内用户已有的 `.venv`；它不会作为普通代码输入复制，也不能当作无用缓存删除。

1. 确认 `python.venv_supported=true`，并优先选择 `ensurepip_supported=true` 的解释器。
2. 用已绑定的 `python3` 制定 `operation="generate"`、`network="none"` 的计划，
   命令为 `python3 -I -B -S -m venv {environment}`。
   执行器将此占位符替换为当前项目受控缓存盘的环境目录，相关修复复用同一环境。
3. 按计划先预演。创建环境是真实隔离执行，成功结果由后续启动复用。
   当 ensurepip 不可用时，`--without-pip` 只能创建空环境；须明确补齐包管理能力后才能安装依赖。
4. 后续使用 `$COLINK_ENV_DIR/bin/python -m pip`、`$COLINK_ENV_DIR/bin/python -m pytest` 和应用入口，
   经同一沙盒内的开发 Shell 制定计划。不要先激活用户 Shell 配置，也不要向全局 Python 安装。
5. 安装使用已批准包源；遇到编译依赖或其他域名，按实际失败和用途补充本机授权并重新计划。
   不把“构建失败”自动转成全目录、全网络授权。

相关修复复用同一个开发任务及其受控缓存环境，避免每轮重新安装。缓存计入项目/全局容量，
任务盘和日志也有边界；完成后由应用按已有保留策略回收已证明属于应用的派生数据。
源码、配置、锁文件、用户已有环境、数据库和未完成写入恢复记录不在自动回收范围内。

## 3. 优先复用已有数据库，再绑定项目角色

数据库默认采用“复用已有实例”。检测到 mysql/postgres 不会自动初始化、启动或替换任何实例。
先在本机配置当前 `project_id` 的连接 Profile；连接地址、端口、数据库/Schema 与角色范围必须明确。
凭据由本机配置保存，网页只看到可用状态和授权范围，不把密码放入命令、日志、SQL 示例或源码。

开发角色按用途分开：migration 角色只负责已批准的项目建表/变更，应用角色只拥有该项目所需的
查询、插入、更新与删除权限。已有业务数据库的结构或数据变动要有明确迁移计划；不借用其他项目
账号，不把角色提升为全库管理员，也不自动清空或重建数据库来绕过迁移失败。

先检查客户端，然后做已授权的连接检查，再运行项目版本化 migration。
优先复用项目已有的 Alembic、Flyway、Liquibase 或 SQL 迁移机制；通过受控终端连接 Profile 执行。
读取到端口或成功执行 `SELECT 1` 只证明局部连通，不能替代 migration、CRUD 和应用接口验证。

### MySQL：确认授权只覆盖这个项目

MySQL 的数据库名字带下划线时，数据库级 `GRANT` 不能只靠肉眼检查名称。
默认 `partial_revokes=OFF` 时，库级授权中的 `_` 和 `%` 是通配符；例如
`colink_app` 的未转义授权还可能覆盖其他同形库名。管理员应先检查当前模式，
在该模式下给精确转义的项目库授权，例如以下无密码示例：

```sql
SELECT @@GLOBAL.partial_revokes;
GRANT SELECT, INSERT, UPDATE, DELETE ON `colink\_app`.* TO 'colink_project'@'localhost';
SHOW GRANTS FOR 'colink_project'@'localhost';
```

`partial_revokes=ON` 时库名按字面字符处理。表和存储例程的数据库限定名也按字面处理，
不要把库级通配规则套到所有 `GRANT`。CoLink 在每次数据库作业执行前检查真实模式与授权，
拒绝全局权限、跨库授权、转授权限和继承角色。不要为适应示例自动修改已有实例的全局模式。
具体语义见 [MySQL 官方 GRANT 说明](https://dev.mysql.com/doc/refman/9.7/en/grant.html)。

使用受控客户端执行 SQL 时，可提交 `command=["mysql", "--execute", "SELECT 1"]` 并设置
`database=true`；执行器绑定已在本机保存的端点、项目库和角色，不需要网页拼接用户名或密码。
连接参数覆盖、用户默认配置与缩写选项均被拒绝。迁移仍须按项目版本化机制明确规划。

远程连接使用 TLS 并验证服务器身份。明确配置为无 TLS 的本机人工实例在首次 SHA2 认证时，
执行器请求服务器 RSA 公钥完成密码认证；这不等于整段连接经过 TLS 加密。不要把这种本机测试
方式直接套到远程或生产数据库。详见 [MySQL 连接选项](https://dev.mysql.com/doc/refman/8.4/en/connection-options.html)。

独立人工 MySQL 9.7.1 实例已验证首次认证、两种权限模式、跨库拒绝、真实 CRUD 及重启后数据保留。
这是执行链路的验收；用户自己的连接、迁移和应用业务仍需要按上面的步骤分别验证。

## 4. 用真实证据验证 CRUD 与向量功能

按照“连接 → migration → CRUD → 应用接口”的顺序验证。使用明确属于当前项目的测试数据，
检查插入、读取、更新和删除的实际结果，并记录运行时、实例/Profile、迁移版本与验收结果。
后端进程还活着、端口在监听、健康接口返回成功，也不能单独证明业务数据链路正确。

需要向量能力时，默认推荐 PostgreSQL + pgvector：

1. 先验证 PostgreSQL 客户端和授权连接；已有 MySQL 项目继续保留其数据库，
   新增向量存储由明确的项目需求决定。
2. 在目标数据库查询扩展是否实际可用、是否已安装，例如检查
   `pg_available_extensions` 与 `pg_extension` 中的 `vector`。
3. 如果需要安装扩展，由有权限的本机管理员为明确的目标数据库执行批准的变更。
   Python 的 `pgvector` 包、psql 客户端或一个开放端口都不能替代服务端扩展。
4. 用已批准的项目 migration 建立向量列、维度与索引，再用受控的测试数据验证写入和相似度查询。
   固定的三维示例可以证明数据库运算路径；它只是一组人工向量演示。
5. 真正的 RAG/检索验收还需要实际 Embedding 来源、维度、模型/版本、文本入库与检索结果。
   外部 Embedding API 属于单独的网络与凭据用途授权；未经实际调用，记录为未验证。

不要把人工向量、空表或静态页面宣称为真实业务通过。报告分别写明“工具已检出”“隔离命令通过”
“真实数据库 CRUD/向量查询通过”“应用页面/接口通过”；尚未测试的项保留未验证状态。

## 5. 服务持续运行与数据保留

启动服务后通过 `execution_status` 查看进程、注册端口与 HTTP 健康结果，再用
`execution_output` 增量读取输出。网页刷新/HTTP 请求结束不会立即停止服务；服务闲置 20 分钟后
按租约停止。网络、源内容或运行时变化后须重新制定计划，不能复用旧授权猜测环境状态。

取消、租约结束或清理任务盘，只处理 CoLink 已拥有的进程和派生数据。
数据库 migration、CRUD 和外部 API 已产生的副作用不会因此撤销。
失败时保留具体操作状态和必要摘要，重新连接仍可查询；不要把取消状态或缺少拒绝日志当作完整
安全证明。遇到不确定的数据或归属，保留现场，按具体项目处理。

## 6. 通过注册的 Unix socket 提供本机 HTTP 服务

受控项目进程通过 `COLINK_SERVICE_SOCKET` 指向的注册 Unix socket 提供服务，CoLink 的可信转发器
监听登记的 `127.0.0.1` 端口。转发器检查 socket 的目录和文件身份，以及内核提供的服务进程身份，
确认对端属于当前任务后才转发数据。项目代码自行监听 TCP，即使使用登记端口或 `0.0.0.0`，
也会被拒绝；不要为启动框架直接放宽 TCP 监听权限。

Python 服务需要使用框架支持的 Unix socket 入口。Node 服务在普通 `net/http` 的注册端口
`listen` 调用上有适配器，已用实际 Node HTTP 和 Vite 服务验证；仍须为项目自己的框架和版本
做 HTTP、取消与重启验收。Vite 输出的 Network 地址由启动参数生成，不是局域网监听成功的证据。

Java 21 的 `ServerSocketChannel.open(StandardProtocolFamily.UNIX)` 可以实现这一入口。
下面是 Maven 项目健康响应的教学示例。Maven 只负责构建；运行服务使用同一个已绑定的 JDK。
它展示 Unix socket 和 HTTP 的最小链路，生产服务仍应使用框架的完整请求解析与错误处理。

```java
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

public class UnixHttp {
    public static void main(String[] args) throws Exception {
        Path endpoint = Path.of(System.getenv("COLINK_SERVICE_SOCKET"));
        Files.deleteIfExists(endpoint);
        byte[] body = "{\"status\":\"ok\"}".getBytes(StandardCharsets.UTF_8);
        byte[] headers = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + "Connection: close\r\nContent-Length: " + body.length + "\r\n\r\n")
            .getBytes(StandardCharsets.US_ASCII);
        try (var server = ServerSocketChannel.open(StandardProtocolFamily.UNIX)) {
            server.bind(UnixDomainSocketAddress.of(endpoint));
            while (true) {
                try (SocketChannel connection = server.accept()) {
                    ByteBuffer request = ByteBuffer.allocate(8192);
                    while (request.hasRemaining()) {
                        if (connection.read(request) < 0) break;
                        String text = new String(request.array(), 0, request.position(),
                            StandardCharsets.US_ASCII);
                        if (text.contains("\r\n\r\n")) break;
                    }
                    // 转发器的就绪检查可能建立连接后立即关闭。
                    if (request.position() == 0) continue;
                    ByteBuffer response = ByteBuffer.allocate(headers.length + body.length);
                    response.put(headers).put(body).flip();
                    while (response.hasRemaining()) connection.write(response);
                }
            }
        }
    }
}
```

1. 先检查 JDK/Maven 的路径和版本，在当前项目的任务盘内编译，保留原项目的构建配置与锁定版本。
2. 为服务制定登记端口的计划，先预演，再用已编译的入口启动；环境中的 socket 路径由执行器提供。
3. 在可信转发器的 `127.0.0.1` 地址请求健康接口，确认实际 HTTP 状态和 JSON 内容。
4. 取消服务后查看 `cleanup_verified` 和任务进程收束结果，再启动新任务并重新请求接口。
   Java 可能经过宽限期后强制终止，退出码须如实记录；单看退出码不能代替清理证明。

这条 JDK 通道已用独立的 Maven/Java 21 测试项目完成编译、真实 HTTP、取消和重启验证。
既有 Tomcat/Spring Boot 配置并不会自动获得 Unix socket 支持；需要所选框架/连接器明确提供或
加入相应入口适配后单独验证。普通 TCP 版 Java 服务暂时不能直接作为本机服务运行。

需要自定义安装或复合命令时，使用同一任务沙盒内的开发 Shell。实际安装依赖、导入/运行验证与
命令失败都读取真实退出状态；失败不会自动扩展目录或网络授权。缺少系统工具、未批准包源或
不支持的框架入口，应按具体错误修订计划，保留已有源文件和业务数据。
