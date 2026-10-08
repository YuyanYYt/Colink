"""Redis checks use synthetic replies only; no local Redis or Keychain is touched."""

import json

import pytest

from code_context import redis_connection
from code_context.source_access import SourceAccess, SourceError


def connection(**changes):
    return {
        "kind": "redis",
        "host": "127.0.0.1",
        "port": 6379,
        "user": "default",
        "database": 0,
        "tls": False,
        **changes,
    }


def test_spring_redis_config_resolves_local_env_without_returning_password(tmp_path):
    (tmp_path / ".env").write_text("REDIS_PASSWORD=synthetic-fixture-secret\n")
    (tmp_path / "application.yml").write_text(
        "spring:\n  data:\n    redis:\n      host: localhost\n      port: 6379\n"
        "      database: 2\n      password: ${REDIS_PASSWORD}\n"
    )
    values, unresolved = redis_connection.discover(SourceAccess(tmp_path))
    assert not unresolved
    spring = next(v for v in values if v["database"] == 2)
    assert spring["host"] == "127.0.0.1"
    assert spring["password"] == "synthetic-fixture-secret"
    assert "synthetic-fixture-secret" not in json.dumps(redis_connection.public(spring))
    assert "password" not in redis_connection.public(spring)


@pytest.mark.parametrize(
    "entry",
    [
        "REDIS_URL=redis://user:fixture@remote.invalid:6379/0\n",
        "REDIS_HOST=localhost\nREDIS_PASSWORD=${MISSING}\n",
        "REDIS_HOST=localhost\nREDIS_SSL=${MISSING}\n",
        "REDIS_URL=redis://localhost:6379/-1\n",
    ],
)
def test_unresolved_or_nonlocal_redis_config_is_not_a_connection(tmp_path, entry):
    (tmp_path / ".env").write_text(entry)
    values, unresolved = redis_connection.discover(SourceAccess(tmp_path))
    assert not values and unresolved
    assert "fixture" not in json.dumps(unresolved)


def test_conflicting_redis_credentials_require_local_review(tmp_path):
    (tmp_path / "application.properties").write_text(
        "spring.data.redis.host=localhost\nspring.data.redis.password=first-fixture\n"
    )
    (tmp_path / "application-dev.properties").write_text(
        "spring.data.redis.host=localhost\nspring.data.redis.password=second-fixture\n"
    )
    values, _ = redis_connection.discover(SourceAccess(tmp_path))
    assert len(values) == 1 and values[0]["credential_conflict"]


class ReplySocket:
    def __init__(self, replies):
        self.replies = bytearray(replies)
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def settimeout(self, timeout):
        assert 0 < timeout <= 2

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        value = bytes(self.replies[:count])
        del self.replies[:count]
        return value


def test_redis_probe_only_authenticates_selects_and_pings(monkeypatch):
    sock = ReplySocket(b"+OK\r\n+OK\r\n+PONG\r\n")
    monkeypatch.setattr(redis_connection.socket, "create_connection", lambda *a, **kw: sock)
    redis_connection.probe(connection(user="app", database=2), "fixture-secret")
    assert len(sock.sent) == 3
    assert b"AUTH" in sock.sent[0] and b"SELECT" in sock.sent[1]
    assert sock.sent[2] == b"*1\r\n$4\r\nPING\r\n"


def test_failed_redis_auth_is_sanitized_and_not_followed_by_commands(monkeypatch):
    sock = ReplySocket(b"-WRONGPASS fixture-sensitive-server-message\r\n")
    monkeypatch.setattr(redis_connection.socket, "create_connection", lambda *a, **kw: sock)
    with pytest.raises(SourceError) as error:
        redis_connection.probe(connection(), "fixture-secret")
    assert str(error.value) == "DATABASE_AUTHENTICATION_FAILED"
    assert len(sock.sent) == 1
