"""Exact native dependency policy and real bounded fixed-version probes."""

import json
import sys
from pathlib import Path

import pytest

from code_context import execution_toolchain as module


def _file(root, name):
    path = root / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(b"\xcf\xfa\xed\xfe" + b"synthetic Mach-O fixture")
    path.chmod(0o700)
    return path


def _link(path):
    return f"\t{path} (compatibility version 1.0.0, current version 1.0.0)\n"


def _fake(tmp_path, monkeypatch, graph, rpaths=None):
    calls = []
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module, "_roots", lambda: (tmp_path,))
    monkeypatch.setattr(module, "_trusted", lambda p, n: (str(Path(p).resolve()), None))

    def run(argv, env):
        calls.append((argv, env))
        if argv[0] == "/usr/bin/otool":
            path = str(Path(argv[-1]).resolve())
            if "-L" in argv:
                return path + ":\n" + "".join(_link(p) for p in graph.get(path, []))
            if "-l" in argv:
                return "".join(
                    "Load command 0\n          cmd LC_RPATH\n      cmdsize 48\n         path "
                    + p
                    + " (offset 12)\n"
                    for p in (rpaths or {}).get(path, [])
                )
        return "synthetic tool version 18.6\n"

    monkeypatch.setattr(module, "_run", run)
    return calls


def test_exact_recursive_libraries_preserve_install_names_without_directory_grants(
    tmp_path, monkeypatch
):
    executable = _file(tmp_path, "bin/psql")
    library = _file(tmp_path, "Cellar/example/1.0/lib/libexample.dylib")
    dependency = _file(tmp_path, "Cellar/other/2.0/lib/libother.dylib")
    alias = tmp_path / "opt/example/libexample.dylib"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(library)
    calls = _fake(
        tmp_path,
        monkeypatch,
        {
            str(executable): [str(alias), "/usr/lib/libSystem.B.dylib"],
            str(library): [str(alias), str(dependency)],
        },
    )
    monkeypatch.setenv("COLINK_TEST_PRIVATE_CREDENTIAL", "fixture-must-not-enter-probe")
    result = module.effective_toolchain_fingerprint({"psql": str(executable)})
    assert result["tools"]["psql"]["version"] == "synthetic tool version 18.6"
    assert set(result["read_paths"]) == {str(executable), str(alias), str(library), str(dependency)}
    assert str(tmp_path) not in result["read_paths"]
    assert all(Path(path).is_file() for path in result["read_paths"])
    assert result["system_links"] == ["/usr/lib/libSystem.B.dylib"]
    assert result["read_metadata_paths"] == [str(alias)]
    assert len(result["libraries"]) == 2
    assert len(result["sha256"]) == 64
    assert all("COLINK_TEST_PRIVATE_CREDENTIAL" not in env for _, env in calls)
    assert all(env["HOME"] == "/var/empty" for _, env in calls)
    assert calls[-1][0] == [str(executable), "--no-psqlrc", "--version"]


def test_rpath_loader_executable_and_inherited_paths_resolve_spaces(tmp_path, monkeypatch):
    executable = _file(tmp_path, "app with spaces/bin/node")
    library = _file(tmp_path, "app with spaces/lib/liba.dylib")
    dependency = _file(tmp_path, "app with spaces/lib/other/libb.dylib")
    more = _file(tmp_path, "app with spaces/lib/libc.dylib")
    _fake(
        tmp_path,
        monkeypatch,
        {
            str(executable): ["@rpath/liba.dylib"],
            str(library): ["@loader_path/other/libb.dylib", "@executable_path/../lib/libc.dylib"],
            str(dependency): ["@rpath/libc.dylib"],
        },
        {str(executable): ["@executable_path/../lib"]},
    )
    paths = module.library_read_paths({"node": str(executable)})
    assert set(paths) == {str(p) for p in (executable, library, dependency, more)}


def test_unresolved_non_system_dependency_fails_before_version_execution(tmp_path, monkeypatch):
    executable = _file(tmp_path, "bin/psql")
    calls = _fake(tmp_path, monkeypatch, {str(executable): ["@rpath/missing.dylib"]})
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_LIBRARY_UNRESOLVED"):
        module.effective_toolchain_fingerprint({"psql": str(executable)})
    assert all(argv[0] == "/usr/bin/otool" for argv, env in calls)


def test_physical_link_chain_metadata_does_not_add_target_directory_read_grants(
    tmp_path, monkeypatch
):
    executable = _file(tmp_path, "bin/psql")
    actual = _file(tmp_path, "Cellar/lib/1.0/lib/libactual.dylib")
    physical_link = actual.parent / "libalias.dylib"
    physical_link.symlink_to(actual.name)
    alias_directory = tmp_path / "opt/lib"
    alias_directory.parent.mkdir(parents=True)
    alias_directory.symlink_to("../Cellar/lib/1.0", target_is_directory=True)
    install = alias_directory / "lib/libalias.dylib"
    _fake(tmp_path, monkeypatch, {str(executable): [str(install)]})
    policy = module.library_read_policy({"psql": str(executable)})
    assert policy["read_metadata_paths"] == sorted([str(physical_link), str(alias_directory)])
    assert str(alias_directory) not in policy["read_paths"]
    assert str(actual.parent) not in policy["read_paths"]
    assert policy["read_aliases"] == [str(install)]
    fingerprint = module.effective_toolchain_fingerprint({"psql": str(executable)})
    assert module.toolchain_unchanged(fingerprint, {"psql": str(executable)})


def test_existing_untrusted_first_rpath_candidate_is_not_skipped(tmp_path, monkeypatch):
    installation = tmp_path / "trusted"
    executable = _file(installation, "bin/node")
    unsafe = _file(tmp_path, "project/lib/libexample.dylib")
    safe = _file(installation, "lib/libexample.dylib")
    _fake(
        tmp_path,
        monkeypatch,
        {str(executable): ["@rpath/libexample.dylib"]},
        {str(executable): [str(unsafe.parent), str(safe.parent)]},
    )
    monkeypatch.setattr(module, "_roots", lambda: (installation,))
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_FILE_UNTRUSTED_OR_CHANGED"):
        module.library_read_paths({"node": str(executable)})


def test_version_and_library_identity_change_digest_without_stale_cache(tmp_path, monkeypatch):
    executable = _file(tmp_path, "bin/node")
    library = _file(tmp_path, "lib/libexample.dylib")
    _fake(tmp_path, monkeypatch, {str(executable): [str(library)]})
    first = module.effective_toolchain_fingerprint({"node": str(executable)})
    library.write_bytes(library.read_bytes() + b"changed dependency")
    second = module.effective_toolchain_fingerprint({"node": str(executable)})
    assert first["sha256"] != second["sha256"]
    assert first["libraries"][0]["identity"] != second["libraries"][0]["identity"]


def test_fast_validation_runs_no_process_and_refuses_identity_alias_path_map_or_metadata_change(
    tmp_path, monkeypatch
):
    executable = _file(tmp_path, "bin/psql")
    library = _file(tmp_path, "lib/libexample.dylib")
    second = _file(tmp_path, "lib/second.dylib")
    alias = tmp_path / "opt/libexample.dylib"
    alias.parent.mkdir()
    alias.symlink_to(library)
    _fake(tmp_path, monkeypatch, {str(executable): [str(alias)]})
    paths = {"psql": str(executable)}
    result = module.effective_toolchain_fingerprint(paths)
    original_probe = module._run

    def forbidden(*args):
        raise AssertionError("fast identity validation executed a probe")

    monkeypatch.setattr(module, "_run", forbidden)
    assert module.toolchain_unchanged(result, paths)
    assert not module.toolchain_unchanged(result, {"node": str(executable)})
    changed = json.loads(json.dumps(result))
    changed["tools"]["psql"]["version"] = "edited metadata"
    assert not module.toolchain_unchanged(changed, paths)
    alias.unlink()
    alias.symlink_to(second)
    assert not module.toolchain_unchanged(result, paths)
    alias.unlink()
    alias.symlink_to(library)
    # Restoring a link's text does not restore its inode/birth identity.
    assert not module.toolchain_unchanged(result, paths)
    monkeypatch.setattr(module, "_run", original_probe)
    result = module.effective_toolchain_fingerprint(paths)
    monkeypatch.setattr(module, "_run", forbidden)
    assert module.toolchain_unchanged(result, paths)
    library.write_bytes(library.read_bytes() + b"modified dylib")
    assert not module.toolchain_unchanged(result, paths)


def test_changed_install_name_during_inspection_refuses_fingerprint(tmp_path, monkeypatch):
    executable = _file(tmp_path, "bin/psql")
    first = _file(tmp_path, "lib/first.dylib")
    second = _file(tmp_path, "lib/second.dylib")
    alias = tmp_path / "lib/alias.dylib"
    alias.symlink_to(first)
    _fake(tmp_path, monkeypatch, {str(executable): [str(alias)]})
    original = module._run

    def replacing(argv, env):
        text = original(argv, env)
        if argv[0] == str(executable):
            alias.unlink()
            alias.symlink_to(second)
        return text

    monkeypatch.setattr(module, "_run", replacing)
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_INSTALL_NAME_CHANGED"):
        module.effective_toolchain_fingerprint({"psql": str(executable)})


def test_file_depth_and_size_budgets_refuse_before_expanding_unbounded_policy(
    tmp_path, monkeypatch
):
    executable = _file(tmp_path, "bin/node")
    one = _file(tmp_path, "lib/one.dylib")
    two = _file(tmp_path, "lib/two.dylib")
    _fake(tmp_path, monkeypatch, {str(executable): [str(one)], str(one): [str(two)]})
    monkeypatch.setattr(module, "MAX_DEPTH", 1)
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_CLOSURE_LIMIT"):
        module.library_read_paths({"node": str(executable)})
    monkeypatch.setattr(module, "MAX_DEPTH", 16)
    monkeypatch.setattr(module, "MAX_FILES", 2)
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_CLOSURE_LIMIT"):
        module.library_read_paths({"node": str(executable)})
    monkeypatch.setattr(module, "MAX_FILES", 128)
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 4)
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_CLOSURE_LIMIT"):
        module.library_read_paths({"node": str(executable)})


def test_unknown_tool_or_relative_install_name_is_never_executed(tmp_path, monkeypatch):
    executable = _file(tmp_path, "bin/node")
    calls = _fake(tmp_path, monkeypatch, {str(executable): ["local/libbad.dylib"]})
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_TOOL_UNSUPPORTED"):
        module.effective_toolchain_fingerprint({"custom": str(executable)})
    assert calls == []
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_LIBRARY_UNRESOLVED"):
        module.effective_toolchain_fingerprint({"node": str(executable)})
    assert all(argv[0] == "/usr/bin/otool" for argv, env in calls)


def test_probe_deadline_and_output_cap_are_real_and_do_not_return_sensitive_output(monkeypatch):
    monkeypatch.setattr(module, "PROBE_SECONDS", 0.1)
    env = {"PATH": "/usr/bin:/bin", "HOME": "/var/empty"}
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_PROBE_TIMEOUT"):
        module._run([sys.executable, "-I", "-S", "-c", "import time;time.sleep(2)"], env)
    monkeypatch.setattr(module, "PROBE_SECONDS", 3)
    with pytest.raises(module.ToolchainError, match="TOOLCHAIN_PROBE_OUTPUT_LIMIT") as error:
        module._run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import sys;sys.stdout.write('synthetic-sensitive-value'*10000)",
            ],
            env,
        )
    assert "synthetic-sensitive-value" not in str(error.value)


@pytest.mark.skipif(sys.platform != "darwin", reason="native Mach-O inventory")
def test_installed_postgresql_runtime_has_exact_live_dependency_files(tmp_path):
    from code_context.execution_environment import _fallback

    psql = _fallback("psql")
    if not psql:
        pytest.skip("no installed PostgreSQL client")
    result = module.effective_toolchain_fingerprint({"psql": psql})
    (tmp_path / "toolchain.json").write_text(json.dumps(result, indent=2))
    assert result["tools"]["psql"]["version"].startswith("psql (PostgreSQL)")
    assert result["static_links_only"]
    assert all(Path(path).is_file() for path in result["read_paths"])
    assert all(not Path(path).is_dir() for path in result["read_paths"])
    assert all(item["identity"]["size"] > 0 for item in result["libraries"])
