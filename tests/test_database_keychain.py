import pytest

from code_context import database_keychain
from code_context.source_access import SourceError

REF = "colink-db-" + "a" * 32


def test_keychain_write_uses_native_api_without_secret_in_process_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(database_keychain.sys, "platform", "darwin")
    monkeypatch.setattr(
        database_keychain, "_store", lambda ref, secret: calls.append((ref, secret)) or 0
    )
    database_keychain.store_secret(REF, "fixture-secret")
    assert calls == [(REF, b"fixture-secret")]


@pytest.mark.parametrize(
    "reference,password",
    [("bad", "fixture-secret"), (REF, "x\x00y"), (REF, "x" * 4097), (REF, "喻" * 1366)],
)
def test_invalid_credentials_never_reach_native_api(monkeypatch, reference, password):
    monkeypatch.setattr(
        database_keychain, "_store", lambda *_: pytest.fail("invalid credential reached Keychain")
    )
    with pytest.raises(SourceError, match="^INVALID_DATABASE_CREDENTIAL$"):
        database_keychain.store_secret(reference, password)


@pytest.mark.parametrize("status", [-25299, -25293])
def test_existing_or_denied_keychain_item_is_not_overwritten(monkeypatch, status):
    monkeypatch.setattr(database_keychain.sys, "platform", "darwin")
    monkeypatch.setattr(database_keychain, "_store", lambda *_: status)
    with pytest.raises(SourceError, match="^DATABASE_CREDENTIAL_STORE_FAILED$"):
        database_keychain.store_secret(REF, "fixture-secret")


def test_native_exception_does_not_echo_secret(monkeypatch):
    monkeypatch.setattr(database_keychain.sys, "platform", "darwin")

    def fail(*_):
        raise OSError("fixture-secret")

    monkeypatch.setattr(database_keychain, "_store", fail)
    with pytest.raises(SourceError, match="^DATABASE_CREDENTIAL_STORE_FAILED$"):
        database_keychain.store_secret(REF, "fixture-secret")


def test_native_reader_preserves_newline_credentials(monkeypatch):
    monkeypatch.setattr(database_keychain.sys, "platform", "darwin")
    monkeypatch.setattr(database_keychain, "_read", lambda _: b"fixture-secret\n")
    assert database_keychain.read_secret(REF) == "fixture-secret\n"


def test_reader_validation_and_failures_do_not_echo_secret(monkeypatch):
    monkeypatch.setattr(database_keychain.sys, "platform", "darwin")
    monkeypatch.setattr(database_keychain, "_read", lambda _: b"\xfffixture-secret")
    with pytest.raises(SourceError, match="^DATABASE_CREDENTIAL_UNAVAILABLE$"):
        database_keychain.read_secret(REF)
    with pytest.raises(SourceError, match="^INVALID_DATABASE_CREDENTIAL$"):
        database_keychain.read_secret("fixture-secret")
