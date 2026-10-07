"""Bounded UTF-8 output with stable byte cursors and explicit missing ranges.

Cursors count the normalized UTF-8 log, rather than arbitrary subprocess bytes.
Each stream has an incremental decoder. Invalid bytes become U+FFFD, and a
split character is published only when complete (or when the stream finishes).
Readers have no decoder state, so retries and independent readers are stable.
"""

import codecs
import threading
import time
from collections import deque
from dataclasses import dataclass

MAX_JOB_BYTES = 8 * 1024 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_CHUNKS = 2048


class OutputError(ValueError):
    """Content-free output protocol errors."""


@dataclass(frozen=True)
class _Chunk:
    start: int
    stream: str
    data: bytes

    @property
    def end(self):
        return self.start + len(self.data)


def _prefix(raw: bytes, limit: int) -> bytes:
    end = min(len(raw), max(0, limit))
    while end and end < len(raw) and raw[end] & 0xC0 == 0x80:
        end -= 1
    return raw[:end]


def _suffix(raw: bytes, limit: int) -> bytes:
    start = max(0, len(raw) - max(0, limit))
    while start < len(raw) and raw[start] & 0xC0 == 0x80:
        start += 1
    return raw[start:]


class OutputBuffer:
    def __init__(self, max_bytes=MAX_JOB_BYTES, head_bytes=MAX_READ_BYTES):
        if (
            type(max_bytes) is not int
            or not 4 <= max_bytes <= MAX_JOB_BYTES
            or type(head_bytes) is not int
            or not 0 <= head_bytes <= max_bytes
        ):
            raise OutputError("INVALID_OUTPUT_BUDGET")
        self.max_bytes = max_bytes
        self.head_bytes = head_bytes
        self._capacity = max_bytes
        self._head = deque()
        self._tail = deque()
        self._head_size = 0
        self._tail_size = 0
        self._total = 0
        self._finished = False
        self._state = "running"
        self._condition = threading.Condition(threading.RLock())
        self._decoders = {
            stream: codecs.getincrementaldecoder("utf-8")("replace")
            for stream in ("stdout", "stderr")
        }

    @property
    def retained_bytes(self):
        with self._condition:
            return self._head_size + self._tail_size

    @property
    def total_bytes(self):
        with self._condition:
            return self._total

    @property
    def finished(self):
        with self._condition:
            return self._finished

    def _put(self, target, chunk):
        if not chunk.data:
            return
        if (
            target
            and target[-1].stream == chunk.stream
            and target[-1].end == chunk.start
            and len(target[-1].data) + len(chunk.data) <= MAX_READ_BYTES
        ):
            previous = target.pop()
            target.append(_Chunk(previous.start, chunk.stream, previous.data + chunk.data))
        else:
            target.append(chunk)

    def _trim(self):
        while self._head and self._head_size > self._capacity:
            chunk = self._head.pop()
            self._head_size -= len(chunk.data)
            kept = _prefix(chunk.data, self._capacity - self._head_size)
            if kept:
                self._head.append(_Chunk(chunk.start, chunk.stream, kept))
                self._head_size += len(kept)
        budget = self._capacity - self._head_size
        while self._tail and self._tail_size > budget:
            chunk = self._tail.popleft()
            self._tail_size -= len(chunk.data)
            kept = _suffix(chunk.data, budget - self._tail_size)
            if kept:
                self._tail.appendleft(_Chunk(chunk.end - len(kept), chunk.stream, kept))
                self._tail_size += len(kept)
        # Alternating one-byte streams must not create millions of Python objects.
        while len(self._head) > MAX_CHUNKS // 2:
            self._head_size -= len(self._head.pop().data)
        while len(self._head) + len(self._tail) > MAX_CHUNKS:
            self._tail_size -= len(self._tail.popleft().data)

    def _publish(self, stream, text):
        raw = text.encode("utf-8")
        start = self._total
        self._total += len(raw)
        if start < self.head_bytes:
            kept = _prefix(raw, min(self.head_bytes - start, self._capacity - self._head_size))
            self._put(self._head, _Chunk(start, stream, kept))
            self._head_size += len(kept)
            start += len(kept)
            raw = raw[len(kept) :]
        if raw and self._capacity > self._head_size:
            self._put(self._tail, _Chunk(start, stream, raw))
            self._tail_size += len(raw)
        self._trim()

    def append(self, stream: str, data: bytes):
        if stream not in self._decoders or not isinstance(data, bytes):
            raise OutputError("INVALID_OUTPUT_CHUNK")
        with self._condition:
            if self._finished:
                raise OutputError("OUTPUT_ALREADY_FINISHED")
            for start in range(0, len(data), MAX_READ_BYTES):
                self._publish(
                    stream, self._decoders[stream].decode(data[start : start + MAX_READ_BYTES])
                )
            self._condition.notify_all()

    def set_capacity(self, max_bytes):
        """Coordinator-owned global budget adjustment; cursor history survives."""
        if type(max_bytes) is not int or not 0 <= max_bytes <= self.max_bytes:
            raise OutputError("INVALID_OUTPUT_BUDGET")
        with self._condition:
            self._capacity = max_bytes
            self._trim()
            self._condition.notify_all()

    def clear(self):
        with self._condition:
            self._head.clear()
            self._tail.clear()
            self._head_size = self._tail_size = 0
            self._condition.notify_all()

    def finish(self, state="exited"):
        if not isinstance(state, str) or not state or len(state) > 32:
            raise OutputError("INVALID_OUTPUT_STATE")
        with self._condition:
            if self._finished:
                return
            for stream, decoder in self._decoders.items():
                self._publish(stream, decoder.decode(b"", final=True))
            self._state = state
            self._finished = True
            self._condition.notify_all()

    def read(self, cursor=0, max_bytes=MAX_READ_BYTES, wait_ms=0):
        if (
            type(cursor) is not int
            or cursor < 0
            or type(max_bytes) is not int
            or not 4 <= max_bytes <= MAX_READ_BYTES
            or type(wait_ms) is not int
            or not 0 <= wait_ms <= 30_000
        ):
            raise OutputError("INVALID_OUTPUT_CURSOR")
        deadline = time.monotonic() + wait_ms / 1000
        with self._condition:
            if cursor > self._total:
                raise OutputError("INVALID_OUTPUT_CURSOR")
            while cursor == self._total and not self._finished and wait_ms:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            chunks, omitted = [], []
            position, remaining = cursor, max_bytes
            for chunk in (*self._head, *self._tail):
                if chunk.end <= position:
                    continue
                if remaining <= 0:
                    break
                if chunk.start > position:
                    omitted.append({"start": position, "end": chunk.start})
                    position = chunk.start
                offset = position - chunk.start
                while offset < len(chunk.data) and chunk.data[offset] & 0xC0 == 0x80:
                    offset += 1
                aligned = chunk.start + offset
                if aligned > position:
                    omitted.append({"start": position, "end": aligned})
                    position = aligned
                raw = _prefix(chunk.data[offset:], remaining)
                if not raw:
                    break
                chunks.append(
                    {
                        "stream": chunk.stream,
                        "start": position,
                        "end": position + len(raw),
                        "text": raw.decode("utf-8"),
                    }
                )
                remaining -= len(raw)
                position += len(raw)
            if remaining > 0 and position < self._total:
                # Only advance across a missing suffix; never skip a retained
                # character just because the response has less than four bytes left.
                if not any(chunk.end > position for chunk in (*self._head, *self._tail)):
                    omitted.append({"start": position, "end": self._total})
                    position = self._total
            return {
                "chunks": chunks,
                "next_cursor": position,
                "total_bytes": self._total,
                "retained_bytes": self._head_size + self._tail_size,
                "omitted": omitted,
                "truncated": bool(omitted),
                "eof": self._finished and position == self._total,
                "state": self._state,
            }

    def tail(self, max_bytes=MAX_READ_BYTES):
        """A bounded terminal failure summary, preserving the stream markers."""
        with self._condition:
            return self.read(max(0, self._total - max_bytes), max_bytes=max_bytes)
