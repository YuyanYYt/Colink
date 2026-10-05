"""User-facing commands; tokens are read from the environment, never flags or logs."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

import uvicorn

from code_context import __version__
from code_context.client import RetryableSyncError, SyncClient, SyncError, read_local_status
from code_context.models import FileChange, SyncBatch
from code_context.scanner import ScanError, Scanner
from code_context.server import build_mcp, create_app
from code_context.storage import MirrorError, MirrorStore


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="colink",
        description="CoLink · 连接你的代码（本地同步与只读 MCP）",
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="启动 HTTP 同步接口与 /mcp 端点")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--public-url", default=os.getenv("CODE_CONTEXT_PUBLIC_URL"))
    serve.add_argument("--data-dir", type=Path, default=Path(".code-context"))

    mcp = commands.add_parser("mcp", help="通过 stdio 提供本地镜像的只读 MCP")
    mcp.add_argument("--data-dir", type=Path, default=Path(".code-context"))

    local = commands.add_parser("local", help="单进程启动持续更新的只读 stdio MCP，不开 HTTP 端口")
    local.add_argument("--root", type=Path, required=True)
    local.add_argument("--project", required=True)
    local.add_argument("--data-dir", type=Path, default=Path(".code-context/local"))
    local.add_argument("--reconcile-seconds", type=float, default=60)
    local_status = commands.add_parser("local-status", help="只读查看本机持续镜像的状态")
    local_status.add_argument("--data-dir", type=Path, default=Path(".code-context/local"))

    live = commands.add_parser("live", help="按需直读指定项目的已保存源码，不保留正文镜像")
    live.add_argument("--root", type=Path, required=True)
    live.add_argument("--project", required=True)
    live.add_argument("--data-dir", type=Path, default=Path(".code-context/live"))

    for name in ("desktop-status", "desktop-run"):
        command = commands.add_parser(name, help="CoLink 菜单栏应用的本机生命周期接口")
        command.add_argument("--workspace", type=Path, default=Path.cwd())
        command.add_argument("--root", type=Path, required=True)
        if name == "desktop-run":
            command.add_argument("--client", required=True)
            command.add_argument("--app-pid", type=int, default=0)

    setup = commands.add_parser("desktop-setup", help="通过标准输入保存首次连接设置，不联网或启动")
    setup.add_argument("--workspace", type=Path, default=Path.cwd())

    tunnel_init = commands.add_parser("tunnel-init", help="生成无凭据的官方隧道配置，不启动连接")
    tunnel_init.add_argument("--root", type=Path, required=True)
    tunnel_init.add_argument("--project", required=True)
    tunnel_init.add_argument("--data-dir", type=Path, default=Path(".code-context/local"))
    tunnel_init.add_argument("--tunnel-id", required=True)
    tunnel_init.add_argument(
        "--output", type=Path, default=Path(".code-context/tunnel/profile.yaml")
    )
    for name in ("tunnel-doctor", "tunnel-run", "tunnel-status"):
        command = commands.add_parser(
            name,
            help={
                "tunnel-doctor": "检查官方隧道配置、网络与权限（会访问 OpenAI）",
                "tunnel-run": "启动官方出站隧道与指定项目的只读 MCP",
                "tunnel-status": "检查本机镜像与隧道的回环健康端点",
            }[name],
        )
        command.add_argument(
            "--profile", type=Path, default=Path(".code-context/tunnel/profile.yaml")
        )
        if name != "tunnel-status":
            command.add_argument("--env-file", type=Path, default=Path(".env.local"))
            command.add_argument("--client", default="tunnel-client")

    for name, help_text in [
        ("scan", "预览允许同步的文件、哈希与排除原因，不上传"),
        ("snapshot", "将指定目录建立为本地镜像快照，不访问网络"),
        ("sync", "同步一次，默认使用 HTTPS，允许本机开发 HTTP"),
        ("watch", "监听修改并同步，断网后保留队列与重试"),
        ("status", "查看本地已确认的同步版本与待发送队列"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--data-dir", type=Path, default=Path(".code-context"))
        if name != "scan":
            command.add_argument("--project", required=True)
        if name in {"sync", "watch", "status"}:
            command.add_argument("--server", default="http://127.0.0.1:8765")
        if name == "watch":
            command.add_argument("--reconcile-seconds", type=float, default=60)
    demo = commands.add_parser("demo", help="运行完整本机演示，验证同步、快照与 MCP")
    demo.add_argument("--output-dir", type=Path, default=Path(".artifacts/demo"))
    demo_local = commands.add_parser(
        "demo-local", help="验证持续 stdio MCP、修改与重启，不访问网络"
    )
    demo_local.add_argument("--output-dir", type=Path, default=Path(".artifacts/demo-local"))
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "desktop-setup":
            from code_context.onboarding import configure_desktop

            payload = sys.stdin.read(65537)
            if len(payload) > 65536:
                raise SyncError("connection settings exceed the size limit")
            emit(configure_desktop(args.workspace, payload))
            return 0
        if args.command.startswith("desktop-"):
            from code_context.desktop import desktop_status, run_desktop

            if args.command == "desktop-status":
                emit(desktop_status(args.workspace, args.root))
                return 0
            return run_desktop(args.workspace, args.root, args.client, args.app_pid)
        if args.command.startswith("tunnel-"):
            from code_context.tunnel import launch_tunnel, prepare_profile, tunnel_status

            if args.command == "tunnel-init":
                emit(
                    prepare_profile(
                        args.root, args.project, args.data_dir, args.tunnel_id, args.output
                    )
                )
                return 0
            if args.command == "tunnel-status":
                emit(tunnel_status(args.profile))
                return 0
            return launch_tunnel(
                args.command.removeprefix("tunnel-"), args.profile, args.env_file, args.client
            )
        if args.command in {"local", "local-status"}:
            from code_context.local import LocalMirror, read_local_mirror_status

            if args.command == "local-status":
                emit(read_local_mirror_status(args.data_dir))
                return 0
            # This server does not need model/control-plane credentials, even when
            # it was started by a tunnel client which inherited them.
            os.environ.pop("CONTROL_PLANE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            with LocalMirror(
                args.root, args.project, args.data_dir, args.reconcile_seconds
            ) as source:
                source.start()
                build_mcp(
                    source.store,
                    args.project,
                    source.ensure_ready,
                    project_names={args.project: source.root.name},
                    status_provider=source.mcp_status,
                ).run(transport="stdio")
            return 0
        if args.command == "live":
            from code_context.live import LiveQueries
            from code_context.source_access import SourceAccess

            os.environ.pop("CONTROL_PLANE_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            source = SourceAccess(
                args.root, excluded_roots=(args.data_dir.expanduser().absolute(),)
            )
            backend = LiveQueries({args.project: source})
            try:
                build_mcp(
                    backend,
                    args.project,
                    project_names={args.project: source.root.name},
                    status_provider=backend.mcp_status,
                ).run(transport="stdio")
            finally:
                backend.close()
            return 0
        if args.command in {"demo", "demo-local"}:
            from code_context.demo import run_demo, run_local_demo

            emit((run_demo if args.command == "demo" else run_local_demo)(args.output_dir))
            return 0
        if args.command in {"serve", "mcp"}:
            store = MirrorStore(args.data_dir / "server" / "mirror.sqlite3")
            if args.command == "mcp":
                build_mcp(store).run(transport="stdio")
            else:
                app = create_app(
                    store,
                    os.getenv("CODE_CONTEXT_READ_TOKEN", ""),
                    os.getenv("CODE_CONTEXT_SYNC_TOKEN", ""),
                    args.public_url,
                )
                uvicorn.run(app, host=args.host, port=args.port, access_log=False)
            return 0
        if args.command == "status":
            emit(read_local_status(args.root, args.project, args.server, args.data_dir))
            return 0
        scanner = Scanner(args.root, excluded_roots=(args.data_dir.expanduser().resolve(),))
        if args.command == "scan":
            scanned = scanner.scan()
            emit(
                {
                    "files": [
                        {"path": f.path, "sha256": f.sha256, "size": len(f.content.encode())}
                        for f in scanned.files.values()
                    ],
                    "skipped": scanned.skipped,
                }
            )
            return 0
        if args.command == "snapshot":
            import uuid

            store = MirrorStore(args.data_dir / "server" / "mirror.sqlite3")
            scanned = scanner.scan()
            try:
                manifest = store.manifest(args.project)
                baseline = {f["path"]: f["sha256"] for f in manifest["files"]}
                revision = manifest["revision"]
            except MirrorError as exc:
                if str(exc) != "project or snapshot not found":
                    raise
                baseline, revision = {}, 0
            changes = [
                FileChange(op="upsert", path=p, content=f.content, sha256=f.sha256)
                for p, f in sorted(scanned.files.items())
                if baseline.get(p) != f.sha256
            ]
            changes.extend(
                FileChange(op="delete", path=p)
                for p in sorted(baseline.keys() - scanned.files.keys())
            )
            if changes or revision == 0:
                result = store.apply(
                    args.project,
                    SyncBatch(
                        request_id=uuid.uuid4().hex,
                        base_revision=revision,
                        mode="delta" if revision else "full",
                        changes=changes,
                    ),
                )
            else:
                result = {"project_id": args.project, "revision": revision}
            emit({**result, "changed_files": len(changes), "skipped": scanned.skipped})
            return 0
        client = SyncClient(
            args.root,
            args.project,
            args.server,
            os.getenv("CODE_CONTEXT_SYNC_TOKEN", ""),
            args.data_dir,
        )
        try:
            if args.command == "sync":
                emit(client.sync_once())
            else:
                emit(
                    {
                        "status": "watching",
                        "project_id": args.project,
                        "root": str(client.root),
                        "server_url": client.server_url,
                    }
                )
                client.watch(emit, reconcile_seconds=args.reconcile_seconds)
        finally:
            client.close()
        return 0
    except KeyboardInterrupt:
        print("已停止；本地待同步队列已保留。", file=sys.stderr)
        return 130
    except (ValueError, SyncError, RetryableSyncError, ScanError, OSError, sqlite3.Error) as exc:
        # All user-facing errors deliberately avoid echoing HTTP headers or rejected content.
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
