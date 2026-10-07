"""Synthetic output; no source content or process execution."""

import threading
import time

import pytest

from code_context.execution_output import MAX_READ_BYTES, OutputBuffer, OutputError


def _all(buffer, cursor=0, max_bytes=64 * 1024):
    chunks, omitted = [], []
    while True:
        page = buffer.read(cursor, max_bytes)
        chunks.extend(page["chunks"])
        omitted.extend(page["omitted"])
        if page["next_cursor"] == cursor or page["eof"]:
            return chunks, omitted, page
        cursor = page["next_cursor"]


def test_utf8_stream_decoders_survive_chunks_and_independent_readers():
    output = OutputBuffer()
    raw = "甲🙂乙".encode()
    output.append("stdout", raw[:2])
    assert output.read()["total_bytes"] == 0
    output.append("stderr", "错".encode()[:1])
    output.append("stdout", raw[2:5])
    output.append("stderr", "错".encode()[1:])
    output.append("stdout", raw[5:])
    output.finish("exited")
    first = _all(output, max_bytes=4)
    second = _all(output, max_bytes=64)
    for stream, expected in (("stdout", "甲🙂乙"), ("stderr", "错")):
        assert "".join(c["text"] for c in first[0] if c["stream"] == stream) == expected
        assert "".join(c["text"] for c in second[0] if c["stream"] == stream) == expected
    assert not first[1]
    assert first[2]["eof"]
    assert first[2]["total_bytes"] == len(("甲🙂乙错").encode())


def test_invalid_and_incomplete_bytes_are_replaced_only_once():
    output = OutputBuffer()
    output.append("stdout", b"ok\xff\xe4")
    assert "".join(c["text"] for c in output.read()["chunks"]) == "ok�"
    output.finish("failed")
    page = output.read()
    assert "".join(c["text"] for c in page["chunks"]) == "ok��"
    assert page["total_bytes"] == 8
    assert page["state"] == "failed"
    output.finish("exited")
    assert output.read() == page
    with pytest.raises(OutputError, match="OUTPUT_ALREADY_FINISHED"):
        output.append("stdout", b"later")


def test_preserves_head_tail_and_reports_exact_missing_byte_range():
    output = OutputBuffer(max_bytes=32, head_bytes=8)
    output.append("stdout", b"H" * 8 + b"m" * 64 + b"T" * 24)
    output.finish()
    chunks, omitted, page = _all(output, max_bytes=8)
    assert "".join(c["text"] for c in chunks) == "H" * 8 + "T" * 24
    assert omitted == [{"start": 8, "end": 72}]
    assert page["total_bytes"] == 96
    assert page["retained_bytes"] == 32
    assert page["next_cursor"] == 96 and page["eof"]


def test_arbitrary_cursor_and_budget_never_split_a_utf8_character():
    output = OutputBuffer(max_bytes=32, head_bytes=4)
    output.append("stdout", "🙂甲乙".encode())
    output.finish()
    chunks, omitted, page = _all(output, cursor=1, max_bytes=4)
    assert "".join(c["text"] for c in chunks) == "甲乙"
    assert omitted == [{"start": 1, "end": 4}]
    assert page["eof"]
    output.set_capacity(0)
    page = output.read()
    assert page["chunks"] == []
    assert page["omitted"] == [{"start": 0, "end": 10}]
    assert page["eof"]


def test_response_limit_and_chunk_metadata_remain_bounded():
    output = OutputBuffer()
    output.append("stdout", b"a" * (9 * 1024 * 1024))
    assert output.retained_bytes <= 8 * 1024 * 1024
    assert sum(len(c["text"].encode()) for c in output.read()["chunks"]) <= MAX_READ_BYTES
    alternating = OutputBuffer(max_bytes=10000, head_bytes=5000)
    for index in range(10000):
        alternating.append("stdout" if index % 2 else "stderr", b"z")
    assert len(alternating._head) + len(alternating._tail) <= 2048
    alternating.finish()
    assert _all(alternating)[1]


def test_wait_releases_lock_and_finish_wakes_a_reader():
    output = OutputBuffer()
    result = []
    reader = threading.Thread(target=lambda: result.append(output.read(wait_ms=1000)))
    reader.start()
    time.sleep(0.02)
    output.append("stdout", b"ready")
    reader.join(1)
    assert not reader.is_alive()
    assert result[0]["chunks"][0]["text"] == "ready"
    reader = threading.Thread(target=lambda: result.append(output.read(5, wait_ms=1000)))
    reader.start()
    output.finish("cancelled")
    reader.join(1)
    assert result[-1]["eof"] and result[-1]["state"] == "cancelled"


@pytest.mark.parametrize("values", [(-1, 4, 0), (1, 4, 0), (0, 3, 0), (0, 65537, 0), (0, 4, -1)])
def test_rejects_invalid_cursors_and_limits(values):
    with pytest.raises(OutputError, match="INVALID_OUTPUT_CURSOR"):
        OutputBuffer().read(*values)
