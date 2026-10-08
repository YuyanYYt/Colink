"""Legacy facts and compressed cache failures have the same bounded contract."""

import json
import zlib

import pytest

from code_context.fact_payloads import FactCacheError, decode_fact
from code_context.intelligence_models import ParsedFile, Relation
from code_context.intelligence_queries import decode_relation
from code_context.live_index import _parse_cache_payload, _parse_cached_file, _relation_payload


def test_compressed_and_legacy_relation_facts_decode_identically():
    fact = Relation("a.py", "CALL", "target", 2, target_path="b.py").to_dict()
    assert decode_relation(_relation_payload(fact)) == fact
    assert decode_relation(json.dumps(fact)) == fact


def test_compressed_and_legacy_parsed_facts_decode_identically():
    fact = ParsedFile("a.py", module="a").to_dict()
    data = json.dumps(fact)
    assert _parse_cached_file(data).to_dict() == fact
    assert _parse_cached_file(_parse_cache_payload(data)).to_dict() == fact


@pytest.mark.parametrize(
    "payload",
    [
        b"CP1:broken",
        b"unknown:" + zlib.compress(b"{}"),
        b"CP1:" + zlib.compress(b"{}")[:-1],
        b"CP1:" + zlib.compress(b"{}") + b"tail",
        b"CP1:" + zlib.compress(b"[]"),
        b"CP1:" + zlib.compress(b"invalid json"),
        "[]",
    ],
)
def test_invalid_cached_payload_is_classified_without_echoing_data(payload):
    with pytest.raises(FactCacheError, match="^invalid derived fact payload$"):
        decode_fact(payload, prefix=b"CP1:", max_bytes=1000)


def test_compressed_parse_limit_uses_the_configured_budget():
    data = json.dumps(ParsedFile("a.py", module="a").to_dict()).encode()
    payload = b"CP1:" + zlib.compress(data)
    assert _parse_cached_file(payload, max_bytes=len(data)).path == "a.py"
    with pytest.raises(FactCacheError):
        _parse_cached_file(payload, max_bytes=len(data) - 1)


def test_compressed_relation_expansion_and_invalid_shape_are_rejected():
    with pytest.raises(FactCacheError):
        decode_relation(b"CL1:" + zlib.compress(b" " * 65_537))
    with pytest.raises(FactCacheError):
        decode_relation("{}")
    with pytest.raises(FactCacheError):
        _parse_cached_file("{}")
