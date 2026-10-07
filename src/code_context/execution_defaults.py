"""Shared uncommon defaults; explicit existing service configuration takes priority."""

DEFAULT_HTTP_PORT = 43116
DEFAULT_DEVELOPMENT_PORTS = (43117, 43118, 43119, 43120, 43121)
DEFAULT_DATABASE_PORTS = {"postgresql": 43122, "pgvector": 43122, "mysql": 43123, "qdrant": 43124}
