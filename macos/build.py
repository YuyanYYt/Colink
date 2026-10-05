"""Build a local, ad-hoc-signed Colink.app; never overwrite an existing app."""

import argparse
import json
import plistlib
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


def build(
    workspace: Path,
    output: Path,
    node: str,
    sharp: str,
    resume: bool = False,
    *,
    client: Path | None = None,
    chat_url: str = "https://chatgpt.com/plugins",
    bundle_id: str = "local.codeconnect.menubar",
    source_mode: str = "mirror",
) -> Path:
    workspace = workspace.resolve()
    output = output.absolute()
    if source_mode not in {"mirror", "live"}:
        raise SystemExit("Source mode must be mirror or live.")
    if output.is_symlink() or (
        output.exists() and (not resume or any(p.is_file() for p in output.rglob("*")))
    ):
        raise SystemExit(
            "Output already exists; choose a new --output path to preserve old builds."
        )
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required")
    client = client or workspace / ".artifacts/tools/tunnel-client-v0.0.15/extracted/tunnel-client"
    client = client.expanduser().absolute()
    if not client.is_file():
        raise SystemExit("Verified official tunnel-client is missing; see docs/LOCAL_HOST.md")
    target = urlsplit(chat_url)
    if target.scheme != "https" or target.netloc != "chatgpt.com":
        raise SystemExit("--chat-url must use https://chatgpt.com")
    metadata_file = workspace / "pyproject.toml"
    version = (
        tomllib.loads(metadata_file.read_text())["project"]["version"]
        if metadata_file.exists()
        else "0.4.0"
    )
    release = re.match(r"^(\d+\.\d+\.\d+)(?:$|[a-z.+-])", version)
    if release is None:
        raise SystemExit("Project version must contain a three-part release number.")
    contents = output / "Contents"
    binary = contents / "MacOS"
    resources = contents / "Resources"
    binary.mkdir(parents=True, exist_ok=resume)
    resources.mkdir(exist_ok=resume)
    sources = workspace / "macos/CodeConnect"
    # Reuse one project-level cache across release/output directories. A new
    # output path must not duplicate hundreds of MiB of SDK module caches.
    module_cache = workspace / "swift-module-cache"
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-swift-version",
            "5",
            "-O",
            "-target",
            "arm64-apple-macosx14.0",
            "-module-cache-path",
            str(module_cache),
            str(sources / "Runtime.swift"),
            str(sources / "Panel.swift"),
            str(sources / "main.swift"),
            "-o",
            str(binary / "CodeConnect"),
        ],
        check=True,
    )
    assets = workspace / "macos/assets"
    for name in ("logo.svg", "menubar.svg"):
        shutil.copy2(assets / name, resources / name)
    subprocess.run(
        [node, str(workspace / "macos/render-icons.cjs"), sharp, str(assets), str(resources)],
        check=True,
    )
    subprocess.run(
        [
            "iconutil",
            "-c",
            "icns",
            str(resources / "CodeConnect.iconset"),
            "-o",
            str(resources / "CodeConnect.icns"),
        ],
        check=True,
    )
    # Paths/URLs only. Never bundle, parse, copy or rewrite the API key.
    runtime = {
        "workspace": str(workspace),
        "uv": uv,
        "client": str(client),
        "sampleRoot": str(workspace / "examples/sample_project"),
        "chatURL": chat_url,
    }
    if source_mode == "live":
        runtime["sourceMode"] = "live"
    (resources / "runtime.json").write_text(
        json.dumps(runtime, ensure_ascii=False, indent=2) + "\n"
    )
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleExecutable": "CodeConnect",
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": "CoLink",
                "CFBundleDisplayName": "CoLink",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": release[1],
                "CoLinkVersion": version,
                "CFBundleVersion": "8",
                "CFBundleIconFile": "CodeConnect",
                "LSApplicationCategoryType": "public.app-category.developer-tools",
                # Declare the menu-bar agent at launch; the installed bundle
                # remains discoverable without a running Dock tile.
                "LSUIElement": True,
                "LSMinimumSystemVersion": "14.0",
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
            },
            stream,
        )
    subprocess.run(["codesign", "--force", "--sign", "-", str(output)], check=True)
    subprocess.run(["codesign", "--verify", "--strict", str(output)], check=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("dist/Colink.app"))
    parser.add_argument(
        "--node",
        default=shutil.which("node") or "node",
    )
    parser.add_argument(
        "--sharp",
        default=str(Path(__file__).resolve().parent / "node_modules/sharp"),
    )
    parser.add_argument("--client", type=Path)
    parser.add_argument("--chat-url", default="https://chatgpt.com/plugins")
    parser.add_argument("--bundle-id", default="local.codeconnect.menubar")
    parser.add_argument("--source-mode", choices=("mirror", "live"), default="mirror")
    parser.add_argument(
        "--resume", action="store_true", help="resume only an empty interrupted build"
    )
    arguments = parser.parse_args()
    print(
        build(
            arguments.workspace,
            arguments.output,
            arguments.node,
            arguments.sharp,
            arguments.resume,
            client=arguments.client,
            chat_url=arguments.chat_url,
            bundle_id=arguments.bundle_id,
            source_mode=arguments.source_mode,
        )
    )
