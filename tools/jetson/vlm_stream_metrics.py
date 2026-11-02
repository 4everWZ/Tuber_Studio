"""Passive native-SSE metrics, composable beneath an existing SSE normalizer.

It does not create a client, request a model, change a payload or launch a process.
Reasoning is retained only as a character count and SHA-256, never as text.
Arrival intervals overlap native compute intervals; they must not be added.
"""

from __future__ import annotations

import hashlib
import json
import math
import time

import httpx

TIMING_FIELDS = (
    "cache_n",
    "prompt_n",
    "prompt_ms",
    "prompt_per_token_ms",
    "prompt_per_second",
    "predicted_n",
    "predicted_ms",
    "predicted_per_token_ms",
    "predicted_per_second",
    "draft_n",
    "draft_n_accepted",
)


class NativeStreamTap(httpx.AsyncByteStream):
    """Observe complete data lines while yielding exactly the original chunks."""

    def __init__(self, inner, record, started, clock):
        self.inner, self.record, self.started, self.clock = inner, record, started, clock
        self.pending = b""
        self.thought_digest = hashlib.sha256()
        self.discarding_line = False
        self.finalized = False
        self.closed = False

    def event(self, line):
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if data == b"[DONE]":
            self.record["done"] = True
            return
        try:
            event = json.loads(data)
        except (ValueError, TypeError, RecursionError):
            self.record["observer_parse_errors"] += 1
            return
        if not isinstance(event, dict):
            self.record["observer_parse_errors"] += 1
            return
        elapsed = (self.clock() - self.started) * 1000
        native = event.get("timings")
        if isinstance(native, dict):
            self.record["native_timings_seen"] = True
            for field in TIMING_FIELDS:
                value = native.get(field)
                numeric = type(value) is int or (type(value) is float and math.isfinite(value))
                if numeric and value >= 0:
                    self.record["native_timings"][field] = value
        usage = event.get("usage")
        if isinstance(usage, dict):
            safe = {
                k: usage[k]
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                if type(usage.get(k)) is int
            }
            for kind, keys in (
                ("prompt_tokens_details", ("cached_tokens",)),
                (
                    "completion_tokens_details",
                    ("reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"),
                ),
            ):
                nested = usage.get(kind)
                if isinstance(nested, dict):
                    safe[kind] = {k: nested[k] for k in keys if type(nested.get(k)) is int}
            self.record["usage"] = safe
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            self.record["observer_parse_errors"] += 1
            return
        for choice in choices:
            if not isinstance(choice, dict):
                self.record["observer_parse_errors"] += 1
                continue
            if isinstance(choice.get("finish_reason"), str):
                self.record["finish_reason"] = choice["finish_reason"]
            delta = choice.get("delta")
            if delta is None:
                delta = {}
            if not isinstance(delta, dict):
                self.record["observer_parse_errors"] += 1
                continue
            thought = delta.get("reasoning_content") or ""
            content = delta.get("content") or ""
            if isinstance(thought, str) and thought:
                self.record["reasoning_characters"] += len(thought)
                self.thought_digest.update(thought.encode("utf-8", errors="surrogatepass"))
                self.record["reasoning_last_delta_ms"] = elapsed
                if thought.strip() and self.record["reasoning_first_nonwhitespace_ms"] is None:
                    self.record["reasoning_first_nonwhitespace_ms"] = elapsed
            if isinstance(content, str) and content:
                self.record["visible_characters"] += len(content)
                self.record["visible_last_delta_ms"] = elapsed
                if content.strip() and self.record["visible_first_nonwhitespace_ms"] is None:
                    self.record["visible_first_nonwhitespace_ms"] = elapsed

    def observe_chunk(self, chunk):
        offset = 0
        while offset < len(chunk):
            end = chunk.find(b"\n", offset)
            stop = len(chunk) if end < 0 else end
            if not self.discarding_line:
                if len(self.pending) + stop - offset > 2_000_000:
                    self.pending = b""
                    self.discarding_line = True
                    self.record["observer_parse_errors"] += 1
                else:
                    self.pending += chunk[offset:stop]
            if end < 0:
                break
            if not self.discarding_line:
                self.event(self.pending)
            self.pending = b""
            self.discarding_line = False
            offset = end + 1

    def finalize(self):
        if not self.finalized:
            self.record["observed_stream_end_ms"] = (self.clock() - self.started) * 1000
            self.record["reasoning_sha256"] = self.thought_digest.hexdigest()
            self.pending = b""
            self.finalized = True

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                self.observe_chunk(chunk)
                yield chunk
            # Only normal EOF completes an unterminated final line. Cancellation,
            # transport errors and early close must not parse a partial event.
            if self.pending and not self.discarding_line:
                self.event(self.pending)
        finally:
            self.finalize()

    async def aclose(self):
        self.finalize()
        if not self.closed:
            self.closed = True
            await self.inner.aclose()


class StageTapTransport(httpx.AsyncBaseTransport):
    """Wrap real HTTP transport inside the already used SSE normalizer."""

    def __init__(self, records, inner=None, clock=time.perf_counter):
        self.records, self.inner, self.clock = records, inner or httpx.AsyncHTTPTransport(retries=0), clock

    async def handle_async_request(self, request):
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return await self.inner.handle_async_request(request)
        started = self.clock()
        try:
            wire_sha256 = hashlib.sha256(request.content).hexdigest()
        except httpx.RequestNotRead:
            # A streaming request is forwarded untouched, not consumed to hash it.
            wire_sha256 = None
        record = {
            "wire_sha256": wire_sha256,
            "request_started_monotonic": started,
            "native_timings_seen": False,
            "native_timings": {field: None for field in TIMING_FIELDS},
            "reasoning_characters": 0,
            "reasoning_sha256": None,
            "reasoning_first_nonwhitespace_ms": None,
            "reasoning_last_delta_ms": None,
            "visible_first_nonwhitespace_ms": None,
            "visible_last_delta_ms": None,
            "visible_characters": 0,
            "usage": None,
            "finish_reason": None,
            "done": False,
            "observer_parse_errors": 0,
            "reasoning_text_saved": False,
            "reasoning_tokens": None,
            "visible_tokens": None,
            "clock_semantics": "Client monotonic arrival times; not isolated device compute. Native prompt_ms includes nested media/prefill work; do not add overlapping intervals.",
        }
        self.records.append(record)
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException as error:
            record["transport_error_type"] = type(error).__name__
            raise
        record["http_status"] = response.status_code
        if "text/event-stream" in response.headers.get("content-type", "").lower():
            response.stream = NativeStreamTap(response.stream, record, started, self.clock)
        else:
            record["stream_not_sse"] = True
        return response

    async def aclose(self):
        await self.inner.aclose()
