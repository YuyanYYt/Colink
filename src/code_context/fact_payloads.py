"""Bounded decoding for disposable code-intelligence facts, never source text."""

import json
import zlib


class FactCacheError(ValueError):
    """A derived payload cannot be decoded and must be regenerated."""


def decode_fact(payload: str | bytes, *, prefix: bytes, max_bytes: int) -> dict:
    """Keep legacy JSON readable and bound expansion of compressed cache rows."""
    try:
        if isinstance(payload, bytes):
            if not payload.startswith(prefix):
                raise ValueError
            inflater = zlib.decompressobj()
            raw = inflater.decompress(payload[len(prefix) :], max_bytes + 1)
            if len(raw) > max_bytes or not inflater.eof or inflater.unused_data:
                raise ValueError
            value = json.loads(raw)
        else:
            value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, zlib.error):
        # No raw payload or decoder message may reach a tool response.
        raise FactCacheError("invalid derived fact payload") from None
