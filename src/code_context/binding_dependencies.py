"""Observed resolver reads, including absent names, for conservative rebinding.

These records describe static lookup inputs, not runtime dependencies. They are
derived metadata in the project's existing SQLite file; no source text is kept.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from weakref import proxy

from code_context.intelligence_models import ParsedFile


def binding_shape(parsed):
    """Every declaration read by Resolver; reference bodies are file-local."""
    return (
        parsed.path,
        parsed.language,
        parsed.module,
        parsed.status,
        tuple(
            (s.name, s.qualname, s.scope, s.kind, s.start_line, tuple(s.parameters))
            for s in parsed.symbols
        ),
        tuple(asdict(s) for s in parsed.scopes),
        tuple(asdict(i) for i in parsed.imports),
        tuple(asdict(b) for b in parsed.bindings),
        tuple(asdict(r) for r in parsed.references if r.kind == "DECORATOR"),
    )


def declaration_summary(parsed):
    """A Resolver input that omits call/reference bodies until actually rebound."""
    return {
        "path": parsed.path,
        "language": parsed.language,
        "status": parsed.status,
        "module": parsed.module,
        **{
            name: [asdict(value) for value in getattr(parsed, name)]
            for name in ("symbols", "scopes", "bindings", "imports")
        },
        "references": [asdict(r) for r in parsed.references if r.kind == "DECORATOR"],
        "diagnostics": parsed.diagnostics,
    }


class ReadMap(defaultdict):
    def __init__(self, name, values, record, factory=None):
        super().__init__(factory, values)
        self.name, self.record = name, record

    def __getitem__(self, key):
        self.record(self.name, key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.record(self.name, key)
        return super().get(key, default)


class ReadSet(set):
    def __init__(self, values, record):
        super().__init__(values)
        self.record = record

    def __contains__(self, key):
        self.record("roots", key)
        return super().__contains__(key)


class BindingReads:
    """One build's memoized lookup digests and per-file read sets."""

    def __init__(self, resolver):
        # ReadMap callbacks retain this tracker. A strong reference back to the
        # resolver would retain the whole build until cyclic GC runs, inflating
        # resident memory across successive local updates.
        self.resolver = proxy(resolver)
        self.active = None
        self.files = {}
        self.digests = {}
        for name in (
            "files",
            "modules",
            "symbols",
            "named",
            "bindings",
            "imports",
            "scope_maps",
            "by_id",
        ):
            value = getattr(resolver, name)
            setattr(
                resolver,
                name,
                ReadMap(name, value, self.record, getattr(value, "default_factory", None)),
            )
        resolver.roots = ReadSet(resolver.roots, self.record)

    def record(self, name, key):
        if self.active is not None:
            encoded = json.dumps([name, key], ensure_ascii=False, separators=(",", ":"))
            self.active[encoded] = self.digest(encoded)

    def digest(self, encoded):
        if encoded in self.digests:
            return self.digests[encoded]
        name, key = json.loads(encoded)
        key = tuple(key) if isinstance(key, list) else key
        if name == "roots":
            value = set.__contains__(self.resolver.roots, key)
        else:
            if name not in {
                "files",
                "modules",
                "symbols",
                "named",
                "bindings",
                "imports",
                "scope_maps",
                "by_id",
            }:
                raise ValueError("invalid binding lookup")
            value = dict.get(getattr(self.resolver, name), key)
            if name == "files" and value is not None:
                value = binding_shape(value)
            elif name == "scope_maps" and value is not None:
                value = {k: asdict(v) for k, v in value.items()}
            elif name in {"symbols", "named", "by_id"}:

                def symbol_value(s):
                    return (s.path, s.name, s.qualname, s.scope, s.kind, s.start_line, s.parameters)

                value = (
                    symbol_value(value)
                    if name == "by_id" and value is not None
                    else [symbol_value(s) for s in value or []]
                    if name != "by_id"
                    else None
                )
            elif name in {"bindings", "imports"}:
                value = [asdict(v) for v in value or []]
            elif name == "modules":
                value = value or []
        digest = hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.digests[encoded] = digest
        return digest

    def unchanged(self, reads):
        return all(self.digest(key) == digest for key, digest in reads.items())


def summary_file(value, path, language):
    parsed = ParsedFile.from_dict(value)
    if parsed.path != path or parsed.language != language:
        raise ValueError("invalid binding summary identity")
    return parsed
