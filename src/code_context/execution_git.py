"""Task-scoped local Git plumbing with private configuration and durable receipts.

No porcelain command runs against an untrusted worktree. Git sees an application
owned GIT_DIR, a private index, sanitized environment, and validated object storage.
Only CoLink's checked ref/index installation changes repository metadata. A commit
contains task changes whose origin matches HEAD and the user's selected index
entries; unrelated staged entries survive the index update.
"""

import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from pathlib import Path

from code_context.local_control import private_directory
from code_context.policy import MAX_FILE_BYTES, SECRET_PATTERNS, excluded_path, validate_path
from code_context.scanner import _identity, _version
from code_context.source_access import SourceError
from code_context.write_coordinator import request_digest, validate_request
from code_context.write_diff import verify_task_files

OID = re.compile(r"[a-f0-9]{40}")
PLAN = re.compile(r"gp_[a-f0-9]{32}")
REF = re.compile(r"refs/heads/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*")
MAX_INDEX = 8 * 1024 * 1024
MAX_METADATA = 32 * 1024 * 1024
TTL = 24 * 3600


class GitError(SourceError):
    """Content-free errors; never forward Git stderr or untrusted repository text."""


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _regular(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == os.geteuid()
        and not info.st_mode & 0o022
    )


def _read(parent, name, limit, *, optional=False):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        if optional:
            return None, None
        raise GitError("GIT_REPOSITORY_REQUIRED: use an existing local Git repository") from None
    except OSError:
        raise GitError(
            "GIT_UNSAFE_METADATA: repository metadata must be real owned files"
        ) from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not _regular(info) or info.st_size > limit:
            raise GitError("GIT_UNSAFE_METADATA: repository metadata exceeds safe bounds")
        raw = stream.read(limit + 1)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            len(raw) > limit
            or _version(info) != _version(os.fstat(stream.fileno()))
            or _version(info) != _version(named)
        ):
            raise GitError("GIT_METADATA_CHANGED: repository metadata changed while reading")
        return raw, list(_version(info))


@contextmanager
def _directory(parent, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        raise GitError(
            "GIT_UNSAFE_REPOSITORY: real owned repository directories are required"
        ) from None
    try:
        info = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or _identity(info) != _identity(named)
        ):
            raise GitError("GIT_UNSAFE_REPOSITORY: repository directory identity is unsafe")
        yield fd
        if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(info):
            raise GitError("GIT_METADATA_CHANGED: repository directory was replaced")
    finally:
        os.close(fd)


class _Repository:
    def __init__(self, source):
        self.source = source
        self.git = source.root / ".git"

    @contextmanager
    def opened(self):
        with self.source.root_fd() as root, _directory(root, ".git") as git:
            yield git

    @contextmanager
    def parent(self, git, path, *, create=False):
        fds, parent = [], git
        try:
            for part in path.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=parent)
                    except FileExistsError:
                        pass
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise GitError("GIT_UNSAFE_METADATA: ref directories must be owned")
                fds.append((parent, part, fd, _identity(info)))
                parent = fd
            yield parent, path.split("/")[-1]
            for parent_fd, part, fd, identity in fds:
                if (
                    _identity(os.fstat(fd)) != identity
                    or _identity(os.stat(part, dir_fd=parent_fd, follow_symlinks=False)) != identity
                ):
                    raise GitError("GIT_METADATA_CHANGED: ref directory identity changed")
        except FileNotFoundError:
            raise GitError("GIT_UNSAFE_METADATA: ref parent directory is missing") from None
        except OSError:
            raise GitError("GIT_UNSAFE_METADATA: ref directories must be real") from None
        finally:
            for _, _, fd, _ in reversed(fds):
                os.close(fd)

    def _objects(self, git):
        # Plumbing never loads project hooks/config/refs, but Git may follow an
        # objects/info/alternates file. Reject alternate object stores and every
        # symlink/hardlink in the bounded object tree before invoking it.
        count, started = 0, time.monotonic()

        def walk(fd, depth):
            nonlocal count
            if depth > 4:
                raise GitError("GIT_UNSAFE_OBJECTS: unsupported object directory structure")
            for item in os.scandir(fd):
                count += 1
                if count > 100_000 or time.monotonic() - started > 5:
                    raise GitError("GIT_OBJECT_SCAN_LIMIT: use a smaller local repository")
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    with _directory(fd, item.name) as child:
                        walk(child, depth + 1)
                elif not _regular(info) or item.name in {"alternates", "http-alternates"}:
                    raise GitError(
                        "GIT_UNSAFE_OBJECTS: aliases and external objects are unsupported"
                    )

        with _directory(git, "objects") as objects:
            identity = list(_identity(os.fstat(objects)))
            walk(objects, 0)
        return identity

    def snapshot(self):
        with self.opened() as git:
            for name in ("commondir", "gitdir", "shallow"):
                if (
                    os.stat(name, dir_fd=git, follow_symlinks=False)
                    if self._exists(git, name)
                    else None
                ):
                    raise GitError(
                        "GIT_UNSUPPORTED_LAYOUT: use a complete ordinary local repository"
                    )
            config, config_version = _read(git, "config", 1024 * 1024, optional=True)
            if config and re.search(rb"(?im)^\s*\[extensions(?:\s|\])", config):
                raise GitError("GIT_UNSUPPORTED_FORMAT: repository extensions are unsupported")
            head_raw, head_version = _read(git, "HEAD", 1024)
            try:
                head = head_raw.decode("ascii").strip()
            except UnicodeError:
                raise GitError("GIT_UNSAFE_HEAD: unsupported HEAD") from None
            ref = "HEAD"
            packed, packed_version = _read(git, "packed-refs", MAX_INDEX, optional=True)
            ref_version = head_version
            if head.startswith("ref: "):
                ref = head[5:]
                if not REF.fullmatch(ref) or any(
                    p.startswith(".") or p.endswith(".") or p.endswith(".lock") or ".." in p
                    for p in ref.split("/")
                ):
                    raise GitError("GIT_UNSAFE_HEAD: unsupported branch reference")
                try:
                    with self.parent(git, ref) as (parent, name):
                        loose, ref_version = _read(parent, name, 128, optional=True)
                except GitError as exc:
                    if str(exc).startswith("GIT_UNSAFE_METADATA: ref parent directory is missing"):
                        loose, ref_version = None, None
                    else:
                        raise
                if loose is not None:
                    try:
                        head = loose.decode("ascii").strip()
                    except UnicodeError:
                        raise GitError("GIT_UNSAFE_HEAD: unsupported branch reference") from None
                else:
                    head = None
                    for line in (packed or b"").splitlines():
                        if line.endswith(b" " + ref.encode()):
                            try:
                                head = line.split(b" ", 1)[0].decode("ascii")
                            except UnicodeError:
                                raise GitError(
                                    "GIT_UNSAFE_HEAD: unsupported packed reference"
                                ) from None
                            break
            if head is not None and OID.fullmatch(head) is None:
                raise GitError("GIT_UNSAFE_HEAD: expected a SHA-1 commit reference")
            index, index_version = _read(git, "index", MAX_INDEX, optional=True)
            return {
                "git_identity": list(_identity(os.fstat(git))),
                "objects_identity": self._objects(git),
                "head": head,
                "ref": ref,
                "head_raw_sha256": _sha(head_raw),
                "head_version": head_version,
                "ref_version": ref_version,
                "packed_sha256": _sha(packed) if packed is not None else None,
                "packed_version": packed_version,
                "index_sha256": _sha(index) if index is not None else None,
                "index_version": index_version,
                "config_sha256": _sha(config) if config is not None else None,
                "config_version": config_version,
            }, index

    @staticmethod
    def _exists(parent, name):
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False


class GitCoordinator:
    """Local-only commits. ``authorize(project_id)`` raises when grant is absent.

    The callback may return a stable JSON grant epoch; plans bind to it. All
    methods additionally enforce the WriteCoordinator's project/source grant.
    An unavailable grant is never inferred from HTTP/chat/session identifiers.
    """

    def __init__(
        self,
        store_root: Path,
        source_for,
        write_coordinator,
        authorize,
        *,
        git_binary="/usr/bin/git",
        clock=time.time,
    ):
        self.directory = private_directory(Path(store_root))
        self.root = self.directory.root
        self.source_for = source_for
        self.write = write_coordinator
        self.authorize = authorize
        self.git_binary = str(git_binary)
        native_git = Path("/Library/Developer/CommandLineTools/usr/bin/git")
        if sys.platform == "darwin" and self.git_binary == "/usr/bin/git" and native_git.is_file():
            self.git_binary = str(native_git)
        if not Path(self.git_binary).is_absolute():
            raise GitError("GIT_TOOLCHAIN_REQUIRED: configure an absolute trusted Git executable")
        self.clock = clock
        self.lock = threading.RLock()
        self.closed = False
        self.lease = None
        with self.directory.root_fd() as parent:
            self.lease = os.open(
                "git.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            if not _regular(os.fstat(self.lease)):
                os.close(self.lease)
                raise GitError("GIT_UNSAFE_STORE: coordinator lease must be a private regular file")
            try:
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.lease)
                raise GitError("GIT_ALREADY_OPEN: stop the current Git coordinator") from None
        self.private = private_directory(self.root / "plumbing")
        self.home = private_directory(self.root / "home")
        for name in ("objects", "refs"):
            private_directory(self.private.root / name)
        self._write_private("HEAD", b"ref: refs/heads/colink\n")
        self._write_private(
            "config",
            b"[core]\nrepositoryformatversion = 0\nbare = true\nignorecase = false\n"
            b"fsmonitor = false\nsplitIndex = false\n[commit]\ngpgSign = false\n",
        )
        with self.directory.root_fd() as fd:
            db_fd = os.open("git.sqlite3", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            info = os.fstat(db_fd)
            os.close(db_fd)
            if not _regular(info) or info.st_mode & 0o077:
                raise GitError("GIT_UNSAFE_STORE: use private regular Git metadata")
        self.db = sqlite3.connect(self.root / "git.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA auto_vacuum=FULL")
        self.db.execute(f"PRAGMA max_page_count={MAX_METADATA // 4096}")
        self.db.execute("CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS receipts("
            "request TEXT PRIMARY KEY, plan TEXT NOT NULL, data TEXT NOT NULL)"
        )
        self.db.commit()

    def _write_private(self, name, raw):
        with self.private.root_fd() as parent:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                if not _regular(os.fstat(stream.fileno())):
                    raise GitError("GIT_UNSAFE_STORE: private Git data changed")
                stream.truncate(0)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(parent)

    def _grant(self, project_id):
        if self.closed:
            raise GitError("GIT_UNAVAILABLE: coordinator is closed")
        epoch = self.authorize(project_id)
        source = self.write._authorized(project_id)
        selected = self.source_for(project_id)
        if selected.source_id != source.source_id:
            raise GitError("GIT_SOURCE_CONFLICT: authorized project identity changed")
        return source, request_digest(epoch)

    def _git(
        self, repository, args, *, index="commit.index", data=None, extra_env=None, limit=MAX_INDEX
    ):
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            raise GitError("GIT_SANDBOX_UNAVAILABLE: native Git isolation is required")
        with repository.opened() as git, _directory(git, "objects") as objects:
            return self._git_in_repository(
                repository, args, objects, index=index, data=data, extra_env=extra_env, limit=limit
            )

    def _git_in_repository(self, repository, args, objects, *, index, data, extra_env, limit):
        # No inherited variables, shell, templates, helpers, user/global/project
        # configuration, filter conversion, replacement refs or interactive input.
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home.root),
            "XDG_CONFIG_HOME": str(self.home.root),
            "LC_ALL": "C",
            "GIT_DIR": str(self.private.root),
            "GIT_WORK_TREE": str(self.private.root),
            "GIT_INDEX_FILE": str(self.private.root / index),
            "GIT_OBJECT_DIRECTORY": str(self.private.root / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": json.dumps(
                str(repository.git / "objects"), ensure_ascii=False
            ),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_PAGER": "/usr/bin/false",
        }
        environment.update(extra_env or {})

        def quote(value):
            return json.dumps(str(value), ensure_ascii=False)

        profile = "\n".join(
            [
                "(version 1)",
                "(deny default)",
                "(allow process-fork)",
                '(allow process-exec (subpath "/usr") (subpath "/bin") '
                '(subpath "/Library/Developer") (subpath "/Applications/Xcode.app"))',
                '(allow file-read* (subpath "/usr") (subpath "/bin") '
                '(subpath "/System") (subpath "/Library/Developer") '
                '(subpath "/Library/Apple") (subpath "/Applications/Xcode.app") '
                '(literal "/Library/Preferences/com.apple.dt.Xcode.plist") '
                '(subpath "/private/var/db/xcode_select_link"))',
                "(allow file-read-metadata)",
                '(allow file-read* (literal "/"))',
                "(allow sysctl-read)",
                '(allow file-read* (literal "/dev/null") (literal "/dev/random") '
                '(literal "/dev/urandom"))',
                '(allow file-write* (literal "/dev/null"))',
                "(allow file-read* "
                + " ".join(
                    "(subpath " + quote(path) + ")"
                    for path in (self.private.root, self.home.root, repository.git / "objects")
                )
                + ")",
                "(allow file-write* "
                + " ".join(
                    "(subpath " + quote(path) + ")" for path in (self.private.root, self.home.root)
                )
                + ")",
                "(deny system-fcntl (fcntl-command 80 110))",
                '(allow mach-lookup (global-name "com.apple.cfprefsd.daemon") '
                '(global-name "com.apple.cfprefsd.agent") '
                '(global-name "com.apple.system.logger"))',
                "(allow signal (target same-sandbox))",
                "(allow process-info* (target same-sandbox))",
            ]
        )
        output, overflow = bytearray(), threading.Event()
        try:
            process = subprocess.Popen(
                [
                    "/usr/bin/sandbox-exec",
                    "-p",
                    profile,
                    self.git_binary,
                    "--no-optional-locks",
                    *args,
                ],
                cwd=self.private.root,
                env=environment,
                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise GitError("GIT_TOOLCHAIN_UNAVAILABLE: trusted Git could not start") from None

        def read_output():
            while chunk := process.stdout.read(8192):
                if len(output) + len(chunk) > limit:
                    overflow.set()
                    process.kill()
                    break
                output.extend(chunk)

        def write_input():
            try:
                process.stdin.write(data)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        writer = None
        if data is not None:
            writer = threading.Thread(target=write_input, daemon=True)
            writer.start()
        try:
            result = process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise GitError(
                "GIT_COMMAND_TIMEOUT: local Git operation exceeded its deadline"
            ) from None
        finally:
            reader.join(timeout=2)
            process.stdout.close()
            if writer:
                writer.join(timeout=2)
        if overflow.is_set() or reader.is_alive():
            raise GitError("GIT_OUTPUT_LIMIT: repository result exceeds safe bounds")
        if result != 0:
            raise GitError("GIT_COMMAND_FAILED: repository format or object state is unsupported")
        return bytes(output)

    def _install_private_index(self, repository, raw, name, head):
        if raw is not None:
            self._write_private(name, raw)
        else:
            with self.private.root_fd() as parent:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
            self._git(
                repository, ["read-tree", head] if head else ["read-tree", "--empty"], index=name
            )

    def _entries(self, repository, head, index_bytes):
        tree = {}
        if head:
            for record in self._git(repository, ["ls-tree", "-rz", head]).split(b"\0"):
                if not record:
                    continue
                metadata, path = record.split(b"\t", 1)
                mode, kind, oid = metadata.decode("ascii").split(" ")
                tree[path.decode("utf-8")] = (mode, oid) if kind == "blob" else (mode, None)
        self._install_private_index(repository, index_bytes, "inspect.index", head)
        staged = {}
        for record in self._git(
            repository, ["ls-files", "--stage", "-z"], index="inspect.index"
        ).split(b"\0"):
            if not record:
                continue
            metadata, path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
            if stage != "0":
                raise GitError("GIT_INDEX_CONFLICT: resolve the existing unmerged index first")
            staged[path.decode("utf-8")] = mode, oid
        return tree, staged

    def _task_files(self, source, project_id, task_id, paths):
        task = self.write._task(project_id, task_id, source)
        if task["state"] not in {"active", "completed"}:
            raise GitError("GIT_TASK_CONFLICT: finish write recovery before planning a commit")
        rows = {row["path"]: row for row in verify_task_files(self.write, task, source)}
        has_moves = self.write.store.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='move_mappings'"
        )
        mappings = (
            {
                row["path"]: row
                for row in self.write.store.query(
                    "SELECT * FROM move_mappings WHERE task_id=?", (task_id,)
                )
            }
            if has_moves
            else {}
        )
        selected = []
        for path in paths:
            self._reject_nested_repository(source, path)
            row = rows.get(path)
            if row is None or row["kind"] not in {"modified", "created"}:
                raise GitError("GIT_TASK_SCOPE: select ordinary files changed by this write task")
            mapping = mappings.get(path)
            origin_path = mapping["origin_path"] if mapping else path
            binary = mapping is not None and mapping["kind"] == "binary"
            moved = origin_path is not None and origin_path != path
            if row["origin_hash"] == row["last_hash"] and not moved:
                raise GitError("GIT_NO_CHANGE: selected file has no task content change")
            if row["last_hash"] is not None:
                if binary:
                    captured = self.write.movement.snapshot(source, path)
                    sha, file_mode = captured.digest, captured.entries[0]["attributes"]["mode"]
                else:
                    document = source.read(path)
                    sha, file_mode = document.sha256, document.mode
                with source.parent_fd(path) as (parent, name):
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if not _regular(info):
                        raise GitError("GIT_UNSAFE_FILE: task files must be regular and unlinked")
                if sha != row["last_hash"]:
                    raise GitError("GIT_TASK_CONFLICT: task content changed")
                mode = "100755" if file_mode & 0o111 else "100644"
            else:
                mode = None
            selected.append(
                {
                    "path": path,
                    "origin_path": origin_path,
                    "binary": binary,
                    "origin_sha256": row["origin_hash"],
                    "sha256": row["last_hash"],
                    "origin_mode": row["origin_mode"],
                    "mode": mode,
                }
            )
            if moved:
                selected.append(
                    {
                        "path": origin_path,
                        "origin_path": origin_path,
                        "binary": binary,
                        "origin_sha256": row["origin_hash"],
                        "origin_mode": row["origin_mode"],
                        "sha256": None,
                        "mode": None,
                    }
                )
        if len({entry["path"] for entry in selected}) != len(selected):
            raise GitError("GIT_MOVE_CONFLICT: selected origins overlap other task paths")
        return sorted(selected, key=lambda entry: entry["path"])

    @staticmethod
    def _reject_nested_repository(source, path):
        with source.root_fd() as root:
            descriptors, parent = [], root
            try:
                for part in path.split("/")[:-1]:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    descriptors.append(fd)
                    parent = fd
                    if _Repository._exists(parent, ".git"):
                        raise GitError(
                            "GIT_NESTED_PROJECT: selected path belongs to a nested repository"
                        )
            finally:
                for fd in reversed(descriptors):
                    os.close(fd)

    def _validate_origins(self, repository, files, tree, staged):
        for entry in files:
            origin_path = entry["origin_path"]
            original = tree.get(origin_path) if origin_path is not None else None
            if staged.get(entry["path"]) != tree.get(entry["path"]):
                raise GitError(
                    "GIT_TASK_ORIGIN_CONFLICT: selected file had existing staged changes"
                )
            if entry["origin_sha256"] is None:
                if tree.get(entry["path"]) is not None:
                    raise GitError(
                        "GIT_TASK_ORIGIN_CONFLICT: created task file already exists in HEAD"
                    )
                continue
            if origin_path != entry["path"] and tree.get(entry["path"]) is not None:
                raise GitError("GIT_TASK_ORIGIN_CONFLICT: move destination already exists in HEAD")
            if staged.get(origin_path) != original:
                raise GitError("GIT_TASK_ORIGIN_CONFLICT: move origin has existing staged changes")
            if original is None or original[0] not in {"100644", "100755"}:
                raise GitError("GIT_TASK_ORIGIN_CONFLICT: task origin does not match HEAD")
            blob = self._git(repository, ["cat-file", "blob", original[1]], limit=MAX_FILE_BYTES)
            expected_mode = "100755" if entry["origin_mode"] & 0o111 else "100644"
            if _sha(blob) != entry["origin_sha256"] or original[0] != expected_mode:
                raise GitError(
                    "GIT_TASK_ORIGIN_CONFLICT: selected file had existing worktree changes"
                )

    def git_plan(
        self,
        project_id,
        task_id,
        *,
        paths,
        message,
        author_name="CoLink",
        author_email="colink@localhost",
    ):
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= 1000
            or any(not isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths)
            or not isinstance(message, str)
            or not 1 <= len(message.encode()) <= 2048
            or "\x00" in message
            or any(pattern.search(message) for pattern in SECRET_PATTERNS)
            or not isinstance(author_name, str)
            or not 1 <= len(author_name) <= 64
            or any(char in author_name for char in "\r\n<>\x00")
            or not isinstance(author_email, str)
            or re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}@[A-Za-z0-9.-]{1,128}", author_email) is None
        ):
            raise GitError("INVALID_GIT_PLAN: use bounded selected paths, message and author")
        try:
            for path in paths:
                validate_path(path)
                if excluded_path(path):
                    raise ValueError
        except ValueError:
            raise GitError(
                "INVALID_GIT_PATH: select permitted project-relative task files"
            ) from None
        with self.lock, self.write.lock:
            source, epoch = self._grant(project_id)
            with source.lock:
                repository = _Repository(source)
                binding, index = repository.snapshot()
                files = self._task_files(source, project_id, task_id, sorted(paths))
                tree, staged = self._entries(repository, binding["head"], index)
                self._validate_origins(repository, files, tree, staged)
                if repository.snapshot()[0] != binding:
                    raise GitError("GIT_PLAN_CONFLICT: HEAD or index changed during planning")
                self._grant(project_id)
                plan_id = "gp_" + uuid.uuid4().hex
                plan = {
                    "id": plan_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "source_id": source.source_id,
                    "epoch": epoch,
                    "created": self.clock(),
                    "binding": binding,
                    "files": files,
                    "selected_paths": sorted(paths),
                    "message": message,
                    "author_name": author_name,
                    "author_email": author_email,
                }
                with self.db:
                    pending = self.db.execute("SELECT data FROM receipts").fetchall()
                    if any(json.loads(row[0])["state"] != "completed" for row in pending):
                        raise GitError(
                            "GIT_RECOVERY_REQUIRED: resolve the pending metadata commit first"
                        )
                    self.db.execute(
                        "DELETE FROM plans WHERE json_extract(data,'$.created') < ?",
                        (self.clock() - TTL,),
                    )
                    self.db.execute(
                        "DELETE FROM receipts WHERE json_extract(data,'$.created') < ? "
                        "AND json_extract(data,'$.state')='completed'",
                        (self.clock() - TTL,),
                    )
                    if self.db.execute("SELECT count(*) FROM plans").fetchone()[0] >= 256:
                        raise GitError("GIT_PLAN_LIMIT: retained plan capacity is full")
                    self.db.execute("INSERT INTO plans VALUES(?,?)", (plan_id, json.dumps(plan)))
                return {
                    "project_id": project_id,
                    "task_id": task_id,
                    "git_plan_id": plan_id,
                    "head": binding["head"],
                    "files": [{"path": f["path"], "sha256": f["sha256"]} for f in files],
                    "message": message,
                    "local_only": True,
                    "expires_at": plan["created"] + TTL,
                    "unrelated_staged_entries_preserved": True,
                }

    def _save_receipt(self, request, plan_id, receipt):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO receipts VALUES(?,?,?)",
                (request, plan_id, json.dumps(receipt)),
            )

    @staticmethod
    def _verify_object(raw, oid):
        try:
            inflater = zlib.decompressobj()
            body = inflater.decompress(raw, MAX_METADATA + 1)
            if (
                len(body) > MAX_METADATA
                or not inflater.eof
                or inflater.unused_data
                or hashlib.sha1(body, usedforsecurity=False).hexdigest() != oid
            ):
                raise ValueError
        except (ValueError, zlib.error):
            raise GitError(
                "GIT_OBJECT_CONFLICT: object contents do not match their identity"
            ) from None

    def _recover_object_temporary(self, repository, request_id, plan_id, receipt):
        """Remove only an install temporary whose private journal proves ownership."""
        intent = receipt.get("object_intent")
        if not intent:
            return
        with repository.opened() as git, _directory(git, "objects") as objects:
            if (
                list(_identity(os.fstat(git))) != intent["git_identity"]
                or list(_identity(os.fstat(objects))) != intent["objects_identity"]
            ):
                raise GitError("GIT_OBJECT_RECOVERY_CONFLICT: repository identity changed")
            with _directory(objects, intent["prefix"]) as parent:
                if list(_identity(os.fstat(parent))) != intent["parent_identity"]:
                    raise GitError("GIT_OBJECT_RECOVERY_CONFLICT: object parent changed")
                try:
                    fd = os.open(
                        intent["temporary"],
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=parent,
                    )
                except FileNotFoundError:
                    fd = None
                except OSError:
                    raise GitError(
                        "GIT_OBJECT_RECOVERY_CONFLICT: unsafe install temporary"
                    ) from None
                if fd is not None:
                    try:
                        info = os.fstat(fd)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.geteuid()
                            or stat.S_IMODE(info.st_mode) not in {0o600, 0o444}
                            or info.st_nlink not in (1, 2)
                            or info.st_size > MAX_INDEX
                            or _identity(info)
                            != _identity(
                                os.stat(intent["temporary"], dir_fd=parent, follow_symlinks=False)
                            )
                        ):
                            raise GitError("GIT_OBJECT_RECOVERY_CONFLICT: unsafe install temporary")
                        identity = intent["identity"]
                        if identity is None:
                            # A crash between create and the durable inode update
                            # can be recovered only after the exact random marker
                            # was written. Empty/unidentified names fail closed.
                            marker = ("colink-object-intent:" + intent["marker"]).encode()
                            if info.st_nlink != 1 or os.read(fd, len(marker) + 1) != marker:
                                raise GitError(
                                    "GIT_OBJECT_RECOVERY_REQUIRED: unconfirmed temporary identity"
                                )
                        elif list(_identity(info))[:2] != identity[:2]:
                            raise GitError(
                                "GIT_OBJECT_RECOVERY_CONFLICT: install temporary changed"
                            )
                        os.unlink(intent["temporary"], dir_fd=parent)
                        os.fsync(parent)
                    finally:
                        os.close(fd)
        receipt["object_intent"] = None
        self._save_receipt(request_id, plan_id, receipt)

    def _copy_objects(self, repository, binding, request_id, plan_id, receipt):
        self._recover_object_temporary(repository, request_id, plan_id, receipt)
        total = 0
        with self.private.root_fd() as private, _directory(private, "objects") as generated:
            with repository.opened() as git, _directory(git, "objects") as objects:
                if (
                    list(_identity(os.fstat(git))) != binding["git_identity"]
                    or list(_identity(os.fstat(objects))) != binding["objects_identity"]
                ):
                    raise GitError("GIT_PLAN_CONFLICT: object destination identity changed")
                for prefix in os.scandir(generated):
                    if re.fullmatch(r"[a-f0-9]{2}", prefix.name) is None:
                        continue
                    with _directory(generated, prefix.name) as source:
                        try:
                            os.mkdir(prefix.name, 0o755, dir_fd=objects)
                        except FileExistsError:
                            pass
                        with _directory(objects, prefix.name) as destination:
                            for entry in os.scandir(source):
                                oid = prefix.name + entry.name
                                if OID.fullmatch(oid) is None:
                                    raise GitError(
                                        "GIT_UNSAFE_OBJECTS: generated object name is invalid"
                                    )
                                raw, _ = _read(source, entry.name, MAX_INDEX)
                                total += len(raw)
                                if total > MAX_METADATA:
                                    raise GitError("GIT_OBJECT_LIMIT: private object cache is full")
                                self._verify_object(raw, oid)
                                current, _ = _read(
                                    destination, entry.name, MAX_INDEX, optional=True
                                )
                                if current is not None:
                                    self._verify_object(current, oid)
                                    continue
                                temporary = ".colink-object-" + uuid.uuid4().hex
                                receipt["object_intent"] = {
                                    "git_identity": binding["git_identity"],
                                    "objects_identity": binding["objects_identity"],
                                    "parent_identity": list(_identity(os.fstat(destination))),
                                    "prefix": prefix.name,
                                    "temporary": temporary,
                                    "marker": uuid.uuid4().hex,
                                    "identity": None,
                                }
                                self._save_receipt(request_id, plan_id, receipt)
                                fd = os.open(
                                    temporary,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600,
                                    dir_fd=destination,
                                )
                                try:
                                    intent = receipt["object_intent"]
                                    marker = ("colink-object-intent:" + intent["marker"]).encode()
                                    self._fill(fd, marker)
                                    os.fsync(destination)
                                    intent["identity"] = list(_identity(os.fstat(fd)))
                                    self._save_receipt(request_id, plan_id, receipt)
                                    self._after_object_temp_created()
                                    self._fill(fd, raw)
                                    os.fchmod(fd, 0o444)
                                    os.fsync(fd)
                                    self._after_object_write()
                                    try:
                                        os.link(
                                            temporary,
                                            entry.name,
                                            src_dir_fd=destination,
                                            dst_dir_fd=destination,
                                            follow_symlinks=False,
                                        )
                                    except FileExistsError:
                                        existing, _ = _read(destination, entry.name, MAX_INDEX)
                                        self._verify_object(existing, oid)
                                    self._after_object_link()
                                    os.unlink(temporary, dir_fd=destination)
                                    os.fsync(destination)
                                    receipt["object_intent"] = None
                                    self._save_receipt(request_id, plan_id, receipt)
                                finally:
                                    os.close(fd)

    def _prepare(self, repository, source, plan, request_id, index):
        self._clear_object_cache()
        self._install_private_index(repository, None, "commit.index", plan["binding"]["head"])
        self._install_private_index(repository, index, "next.index", plan["binding"]["head"])
        updates = bytearray()
        for entry in plan["files"]:
            oid, mode = "0" * 40, "0"
            if entry["sha256"] is not None:
                raw = (
                    self.write.movement.snapshot(source, entry["path"], bodies=True).bodies[""]
                    if entry["binary"]
                    else source.read(entry["path"]).content.encode()
                )
                if _sha(raw) != entry["sha256"]:
                    raise GitError("GIT_TASK_CONFLICT: selected file changed before preparation")
                oid = (
                    self._git(
                        repository,
                        ["hash-object", "-w", "--stdin", "--no-filters"],
                        data=raw,
                        limit=128,
                    )
                    .decode()
                    .strip()
                )
                mode = entry["mode"]
                self._object_cache_usage()
            updates.extend(f"{mode} {oid}\t{entry['path']}".encode() + b"\0")
        for name in ("commit.index", "next.index"):
            self._git(
                repository,
                ["update-index", "-z", "--index-info"],
                index=name,
                data=bytes(updates),
                limit=128,
            )
        tree = self._git(repository, ["write-tree"], limit=128).decode().strip()
        stamp = f"{int(self.clock())} +0000"
        author = {
            f"GIT_{role}_{field}": value
            for role in ("AUTHOR", "COMMITTER")
            for field, value in (
                ("NAME", plan["author_name"]),
                ("EMAIL", plan["author_email"]),
                ("DATE", stamp),
            )
        }
        args = ["commit-tree", tree]
        if plan["binding"]["head"]:
            args += ["-p", plan["binding"]["head"]]
        commit = (
            self._git(repository, args, data=plan["message"].encode(), extra_env=author, limit=128)
            .decode()
            .strip()
        )
        if OID.fullmatch(commit) is None:
            raise GitError("GIT_COMMAND_FAILED: no commit object was produced")
        self._grant(plan["project_id"])
        with self.private.root_fd() as parent:
            next_index, _ = _read(parent, "next.index", MAX_INDEX)
        receipt = {
            "state": "copying_objects",
            "created": self.clock(),
            "commit_id": commit,
            "index_sha256": _sha(next_index),
            "marker": uuid.uuid4().hex,
            "index_lock_identity": None,
            "ref_lock_identity": None,
            "object_intent": None,
        }
        self._save_receipt(request_id, plan["id"], receipt)
        self._copy_objects(repository, plan["binding"], request_id, plan["id"], receipt)
        receipt["state"] = "prepared"
        self._save_receipt(request_id, plan["id"], receipt)
        return receipt

    def _object_cache_usage(self, *, enforce=True):
        total = 0
        with self.private.root_fd() as private, _directory(private, "objects") as objects:
            for prefix in os.scandir(objects):
                if re.fullmatch(r"[a-f0-9]{2}", prefix.name) is None:
                    raise GitError(
                        "GIT_UNSAFE_STORE: private object cache contains unknown entries"
                    )
                with _directory(objects, prefix.name) as child:
                    for entry in os.scandir(child):
                        if OID.fullmatch(prefix.name + entry.name) is None or not _regular(
                            entry.stat(follow_symlinks=False)
                        ):
                            raise GitError(
                                "GIT_UNSAFE_STORE: private object cache contains unsafe entries"
                            )
                        total += entry.stat(follow_symlinks=False).st_size
                        if enforce and total > MAX_METADATA:
                            raise GitError("GIT_OBJECT_LIMIT: private Git cache is full")
        return total

    def _clear_object_cache(self):
        self._object_cache_usage(enforce=False)
        # These are only app-owned generated Git objects from the previous
        # completed preparation. Pending receipts prevent a new preparation.
        with self.private.root_fd() as private, _directory(private, "objects") as objects:
            for prefix in os.scandir(objects):
                with _directory(objects, prefix.name) as child:
                    for entry in os.scandir(child):
                        os.unlink(entry.name, dir_fd=child)

    @staticmethod
    def _lock_file(parent, name, marker, identity=None):
        try:
            fd = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            os.write(fd, marker)
            os.fsync(fd)
            os.fsync(parent)
            return fd
        except FileExistsError:
            raw, _ = _read(parent, name, MAX_INDEX)
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
            if (identity is None and raw != marker) or (
                identity is not None and list(_identity(os.fstat(fd))) != identity
            ):
                os.close(fd)
                raise GitError("GIT_LOCKED: another Git operation owns the metadata lock") from None
            return fd

    @staticmethod
    def _fill(fd, raw):
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        while raw:
            written = os.write(fd, raw)
            raw = raw[written:]
        os.fsync(fd)

    def _after_ref_install(self):
        """Crash-injection seam: the durable journal already identifies the commit."""

    def _after_object_temp_created(self):
        """Crash-injection seam after the private journal binds the temporary inode."""

    def _after_object_write(self):
        """Crash-injection seam after the compressed object is durably filled."""

    def _after_object_link(self):
        """Crash-injection seam before the owned object's second link is removed."""

    def _record_reflogs(self, repository, plan, receipt):
        binding = plan["binding"]
        old = binding["head"] or "0" * 40
        new = receipt["commit_id"]
        subject = "".join(char for char in plan["message"].splitlines()[0] if ord(char) >= 32)
        line = (
            f"{old} {new} {plan['author_name']} <{plan['author_email']}> "
            f"{int(receipt['created'])} +0000\tcommit (CoLink): {subject}\n"
        ).encode()
        try:
            with repository.opened() as git:
                for path in dict.fromkeys(["logs/HEAD", "logs/" + binding["ref"]]):
                    with repository.parent(git, path, create=True) as (parent, name):
                        previous, previous_version = _read(parent, name, MAX_INDEX, optional=True)
                        if any(
                            record.startswith(f"{old} {new} ".encode())
                            for record in (previous or b"").splitlines()
                        ):
                            continue
                        fd = os.open(
                            name,
                            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                            0o600,
                            dir_fd=parent,
                        )
                        try:
                            info = os.fstat(fd)
                            if (
                                not _regular(info)
                                or info.st_size + len(line) > MAX_INDEX
                                or (
                                    previous_version is not None
                                    and list(_version(info)) != previous_version
                                )
                            ):
                                raise GitError("GIT_REFLOG_CONFLICT: reflog changed")
                            if os.write(fd, line) != len(line):
                                raise GitError(
                                    "GIT_REFLOG_FAILED: bounded reflog append was incomplete"
                                )
                            os.fsync(fd)
                            if _identity(
                                os.stat(name, dir_fd=parent, follow_symlinks=False)
                            ) != _identity(info):
                                raise GitError("GIT_REFLOG_CONFLICT: reflog identity changed")
                            os.fsync(parent)
                        finally:
                            os.close(fd)
            return True
        except (GitError, OSError):
            # HEAD/index remain journaled and are never undone because optional
            # user history metadata cannot be safely appended.
            return False

    def _install(self, repository, plan, request, receipt):
        binding = plan["binding"]
        now, _ = repository.snapshot()
        if (
            now["git_identity"] != binding["git_identity"]
            or now["objects_identity"] != binding["objects_identity"]
        ):
            raise GitError("GIT_RECOVERY_CONFLICT: repository identity changed")
        current_head = now["head"]
        if current_head == receipt["commit_id"] and now["index_sha256"] == receipt["index_sha256"]:
            receipt["reflog_recorded"] = self._record_reflogs(repository, plan, receipt)
            receipt["state"] = "completed"
            self._save_receipt(request, plan["id"], receipt)
            return
        ref_done = current_head == receipt["commit_id"]
        if not ref_done and now != binding:
            raise GitError("GIT_PLAN_CONFLICT: HEAD or index changed before commit")
        if ref_done and (
            now["ref"] != binding["ref"]
            or now["index_sha256"] != binding["index_sha256"]
            or now["index_version"] != binding["index_version"]
        ):
            raise GitError("GIT_RECOVERY_CONFLICT: committed HEAD has an externally changed index")
        with self.private.root_fd() as private:
            next_index, _ = _read(private, "next.index", MAX_INDEX)
        if _sha(next_index) != receipt["index_sha256"]:
            raise GitError("GIT_RECOVERY_CONFLICT: prepared private index changed")
        marker = ("colink-git-lock:" + receipt["marker"]).encode()
        with (
            repository.opened() as git,
            repository.parent(git, binding["ref"], create=True) as (ref_parent, ref_name),
        ):
            index_fd = self._lock_file(git, "index.lock", marker, receipt["index_lock_identity"])
            ref_fd = None
            try:
                receipt["index_lock_identity"] = list(_identity(os.fstat(index_fd)))
                if not ref_done:
                    ref_fd = self._lock_file(
                        ref_parent, ref_name + ".lock", marker, receipt["ref_lock_identity"]
                    )
                    receipt["ref_lock_identity"] = list(_identity(os.fstat(ref_fd)))
                self._save_receipt(request, plan["id"], receipt)
                checked, _ = repository.snapshot()
                if (not ref_done and checked != binding) or (ref_done and checked != now):
                    raise GitError("GIT_PLAN_CONFLICT: metadata changed while acquiring locks")
                self._grant(plan["project_id"])
                if not ref_done:
                    source = self.source_for(plan["project_id"])
                    current = self._task_files(
                        source,
                        plan["project_id"],
                        plan["task_id"],
                        plan["selected_paths"],
                    )
                    if current != plan["files"]:
                        raise GitError(
                            "GIT_TASK_CONFLICT: source changed during commit preparation"
                        )
                self._fill(index_fd, next_index)
                if not ref_done:
                    self._fill(ref_fd, (receipt["commit_id"] + "\n").encode())
                    os.replace(
                        ref_name + ".lock", ref_name, src_dir_fd=ref_parent, dst_dir_fd=ref_parent
                    )
                    os.fsync(ref_parent)
                    self._after_ref_install()
                os.replace("index.lock", "index", src_dir_fd=git, dst_dir_fd=git)
                os.fsync(git)
                receipt["reflog_recorded"] = self._record_reflogs(repository, plan, receipt)
                receipt["state"] = "completed"
                self._save_receipt(request, plan["id"], receipt)
            finally:
                os.close(index_fd)
                if ref_fd is not None:
                    os.close(ref_fd)

    def git_commit(self, project_id, git_plan_id, request_id):
        validate_request(request_id)
        if not isinstance(git_plan_id, str) or PLAN.fullmatch(git_plan_id) is None:
            raise GitError("INVALID_GIT_PLAN_ID: use an issued Git plan")
        with self.lock, self.write.lock:
            source, epoch = self._grant(project_id)
            row = self.db.execute("SELECT data FROM plans WHERE id=?", (git_plan_id,)).fetchone()
            if not row:
                raise GitError("GIT_PLAN_EXPIRED: obtain a fresh plan")
            plan = json.loads(row[0])
            if plan["project_id"] != project_id or plan["source_id"] != source.source_id:
                raise GitError("GIT_PLAN_SCOPE: plan belongs to another project or source")
            prior = self.db.execute(
                "SELECT plan,data FROM receipts WHERE request=?", (request_id,)
            ).fetchone()
            if prior and prior[0] != git_plan_id:
                raise GitError("REQUEST_ID_CONFLICT: request belongs to a different Git plan")
            receipt = json.loads(prior[1]) if prior else None
            duplicate = receipt is not None
            if receipt is None and (self.clock() - plan["created"] > TTL or plan["epoch"] != epoch):
                raise GitError("GIT_PLAN_EXPIRED: authorization or retained plan changed")
            with source.lock:
                repository = _Repository(source)
                if receipt is None:
                    pending = self.db.execute("SELECT data FROM receipts").fetchall()
                    if any(json.loads(row[0])["state"] != "completed" for row in pending):
                        raise GitError(
                            "GIT_RECOVERY_REQUIRED: resolve the pending metadata commit first"
                        )
                    binding, index = repository.snapshot()
                    if binding != plan["binding"]:
                        raise GitError("GIT_PLAN_CONFLICT: HEAD or index changed since planning")
                    files = self._task_files(
                        source, project_id, plan["task_id"], plan["selected_paths"]
                    )
                    if files != plan["files"]:
                        raise GitError("GIT_TASK_CONFLICT: task files changed since planning")
                    tree, staged = self._entries(repository, binding["head"], index)
                    self._validate_origins(repository, files, tree, staged)
                    receipt = self._prepare(repository, source, plan, request_id, index)
                if receipt["state"] == "copying_objects":
                    self._recover_object_temporary(repository, request_id, plan["id"], receipt)
                    if repository.snapshot()[0] != plan["binding"]:
                        raise GitError("GIT_PLAN_CONFLICT: metadata changed during object install")
                    if (
                        self._task_files(
                            source, project_id, plan["task_id"], plan["selected_paths"]
                        )
                        != plan["files"]
                    ):
                        raise GitError("GIT_TASK_CONFLICT: task changed during object install")
                    self._grant(project_id)
                    self._copy_objects(repository, plan["binding"], request_id, plan["id"], receipt)
                    receipt["state"] = "prepared"
                    self._save_receipt(request_id, plan["id"], receipt)
                if receipt["state"] != "completed":
                    self._install(repository, plan, request_id, receipt)
                return {
                    "project_id": project_id,
                    "task_id": plan["task_id"],
                    "git_plan_id": git_plan_id,
                    "commit_id": receipt["commit_id"],
                    "state": "completed",
                    "duplicate": duplicate,
                    "local_only": True,
                    "unrelated_staged_entries_preserved": True,
                    "reflog_recorded": receipt.get("reflog_recorded", False),
                }

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.db.close()
                fcntl.flock(self.lease, fcntl.LOCK_UN)
                os.close(self.lease)
