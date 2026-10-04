"""Fail closed on common private data in the Git index or a release bundle.

This is a release guard, not a complete DLP/secret scanner. Findings only name
files and rule identifiers; input contents and credentials are never printed.
"""

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

RULES = {
    "private-home-path": re.compile(rb"/Users/[A-Za-z0-9_-]+/"),
    "personal-chat-link": re.compile(rb"chatgpt\.com/c/[0-9a-f-]{24,}"),
    "personal-plugin-id": re.compile(rb"asdk_app_(?:v_)?[0-9a-f]{24,}"),
    "personal-tunnel-id": re.compile(rb"tunnel_[0-9a-f]{24,}"),
    "literal-api-key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
FORBIDDEN_PARTS = {
    ".code-context",
    ".artifacts",
    ".venv",
    "swift-module-cache",
    "node_modules",
    "__pycache__",
    ".app.json",
}
PEM_BLOCK = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\r\n]+"
    rb"[A-Za-z0-9+/=\r\n]+-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)
# Exact built-in strings from the checksum-verified official 0.0.15 archive,
# independently compared before our ad-hoc re-signing. These are upstream
# binary contents, not a user's saved connection or runtime credential.
# Waivers are scoped by component, rule and content hash, never by directory.
REVIEWED_UPSTREAM = {
    ("Contents/Resources/tunnel-client/cloudflared", "private-key"): {
        "17ac4c2ca3deb87202cd2c101d4da9d4f4c0ec3d51a0381729329102b28e61a4"
    },
    ("Contents/Resources/tunnel-client/tunnel-client", "personal-tunnel-id"): {
        "00e056b9c41a27323f02c87636e1bbbddeeac3fb8e726566a70303318e057898",
        "a5bf9a3347f9414c601003f7fc7d23a7d95557cd77b15c4ae345e17c8ef4c8e5",
        "3621d4efcf4b8adbd339e70f5def85bca69fa11b78b48a652fc46cc29ae17bc0",
    },
}


def inspect(name: str, data: bytes, *, bundle: bool = False) -> list[str]:
    path = Path(name)
    problems = []
    if not bundle and (
        any(part in FORBIDDEN_PARTS or part.endswith(".app") for part in path.parts)
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.suffix in {".sqlite3", ".db", ".log", ".zip", ".dmg", ".pkg", ".pem", ".key"}
        or name.startswith("integrations/chatgpt-plugin/.codex-plugin/")
        or name == "integrations/chatgpt-plugin/plugin.json"
    ):
        problems.append("private-or-generated-file")
    if bundle and (path.name.startswith(".env") or ".code-context" in path.parts):
        problems.append("runtime-user-data-in-bundle")
    for label, pattern in RULES.items():
        if bundle and label == "private-key":
            pattern = PEM_BLOCK
        for match in pattern.finditer(data):
            # Deliberately fake zero IDs used by tests are not live connections.
            if label == "personal-tunnel-id" and set(match.group()[7:]) == {ord("0")}:
                continue
            if bundle and label == "private-home-path":
                # Upstream binaries may retain build-host paths. Reject this
                # maintainer's home rather than mislabel all upstream symbols.
                if not match.group().startswith(("/Users/" + Path.home().name + "/").encode()):
                    continue
            if bundle and hashlib.sha256(match.group()).hexdigest() in REVIEWED_UPSTREAM.get(
                (name, label), set()
            ):
                continue
            problems.append(label)
            break
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    findings = []
    count = 0
    if args.bundle:
        root = args.bundle.resolve()
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                if not path.resolve().is_relative_to(root):
                    findings.append((str(path.relative_to(root)), ["external-bundle-symlink"]))
                continue
            if path.is_file():
                count += 1
                rules = inspect(str(path.relative_to(root)), path.read_bytes(), bundle=True)
                if rules:
                    findings.append((str(path.relative_to(root)), rules))
    else:
        names = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"])
        # For subsequent commits also check already tracked files, not just the diff.
        names += subprocess.check_output(["git", "ls-files", "-z"])
        for name in sorted(set(names.decode().strip("\0").split("\0")) - {""}):
            count += 1
            data = subprocess.check_output(["git", "show", ":" + name])
            rules = inspect(name, data)
            if rules:
                findings.append((name, rules))
    for name, rules in findings:
        print(name + ": " + ", ".join(rules), file=sys.stderr)
    print(f"Publication guard: {count} files, {len(findings)} findings")
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
