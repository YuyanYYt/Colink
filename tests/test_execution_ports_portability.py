"""An unavailable optional native port capability preserves read-only startup."""

from types import SimpleNamespace

import pytest

import code_context.execution_ports as implementation
from code_context.execution_ports import NativeProcesses, PortError, PortsCoordinator
from code_context.source_access import SourceAccess


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("fixture\n")
    source = SourceAccess(root)
    execution = SimpleNamespace(
        state=SimpleNamespace(root=tmp_path / "state"),
        grants={},
        authorize=lambda _: {"epoch": "fixture", "source_id": source.source_id},
    )
    return source, execution


def test_other_platform_keeps_coordinator_constructible_and_source_readable(inputs, monkeypatch):
    monkeypatch.setattr(implementation.sys, "platform", "linux")
    source, execution = inputs
    coordinator = PortsCoordinator(lambda _: source, execution, lambda: True)
    try:
        assert coordinator.native is None
        assert coordinator.pending_confirmations() == []
        assert (source.root / "main.py").read_text() == "fixture\n"
        with pytest.raises(PortError, match="PORT_NATIVE_UNAVAILABLE"):
            coordinator.status("fixture")
    finally:
        coordinator.close()


@pytest.mark.parametrize("failure", ["library_missing", "entrypoint_missing"])
def test_missing_native_library_or_entrypoint_is_optional_and_content_free(
    inputs, monkeypatch, failure
):
    monkeypatch.setattr(implementation.sys, "platform", "darwin")

    def unavailable(*args, **kwargs):
        if failure == "library_missing":
            raise OSError("fixture-secret-bearing-loader-error")
        return SimpleNamespace()

    monkeypatch.setattr(implementation.ctypes, "CDLL", unavailable)
    monkeypatch.setattr(
        implementation.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            "no command fallback for unavailable native inspection"
        ),
    )
    source, execution = inputs
    coordinator = PortsCoordinator(lambda _: source, execution, lambda: True)
    try:
        assert coordinator.native is None
        for operation in (
            lambda: coordinator.status("fixture"),
            lambda: coordinator.plan_release("fixture", 12345),
            lambda: coordinator.confirm_release("pp_" + "a" * 32),
            lambda: coordinator.release("fixture", "pp_" + "a" * 32, "fixture_release_request"),
        ):
            with pytest.raises(PortError, match="PORT_NATIVE_UNAVAILABLE") as error:
                operation()
            assert "fixture-secret" not in str(error.value)
        assert coordinator.pending_confirmations() == []
        assert coordinator.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
    finally:
        coordinator.close()


def test_native_adapter_requires_supported_platform_before_loading_library(monkeypatch):
    monkeypatch.setattr(implementation.sys, "platform", "linux")
    monkeypatch.setattr(
        implementation.ctypes,
        "CDLL",
        lambda *args, **kwargs: pytest.fail("unsupported platform must not load libproc"),
    )
    with pytest.raises(PortError, match="PORT_NATIVE_UNAVAILABLE"):
        NativeProcesses()
