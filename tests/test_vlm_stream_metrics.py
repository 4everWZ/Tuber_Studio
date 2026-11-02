"""Passive observer tests use only MockTransport; no model or platform writes."""

import asyncio
import hashlib
import json

import httpx
import pytest

from tools.jetson.vlm_stream_metrics import NativeStreamTap, StageTapTransport


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closes = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closes += 1


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value


def data(event, ending=b"\n\n"):
    return b"data: " + json.dumps(event, ensure_ascii=False).encode() + ending


async def observe(chunks):
    records = []
    inner = Chunks(chunks)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=inner, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(
        transport=StageTapTransport(records, httpx.MockTransport(handler))
    ) as client:
        async with client.stream(
            "POST", "http://mock/v1/chat/completions", content=b'{"stream":true}'
        ) as response:
            received = [chunk async for chunk in response.aiter_raw()]
    assert received == chunks
    assert len(requests) == 1
    assert requests[0].content == b'{"stream":true}'
    assert inner.closes == 1
    return records[0]


@pytest.mark.asyncio
async def test_utf8_split_chunks_final_native_statistics_and_reasoning_privacy():
    thought = "仅用于测试的思考🐟"
    raw = (
        data({"choices": [{"delta": {"reasoning_content": thought}}]}, b"\r\n\r\n")
        + data({"choices": [{"delta": {"content": "画面是聊天界面"}, "finish_reason": "stop"}]})
        + data(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 350,
                    "completion_tokens": 50,
                    "total_tokens": 400,
                    "private_unexpected": thought,
                    "completion_tokens_details": {"reasoning_tokens": 12, "unknown": thought},
                },
                "timings": {
                    "cache_n": 0,
                    "prompt_n": 350,
                    "prompt_ms": 120,
                    "predicted_n": 50,
                    "predicted_ms": 1000,
                    "draft_n": 30,
                    "draft_n_accepted": 22,
                },
            }
        )
        + b"data: [DONE]\n\n"
    )
    row = await observe([raw[i : i + 1] for i in range(len(raw))])
    assert row["wire_sha256"] == hashlib.sha256(b'{"stream":true}').hexdigest()
    assert row["native_timings"]["draft_n_accepted"] == 22
    assert row["native_timings"]["cache_n"] == 0
    assert row["native_timings_seen"] and row["done"]
    assert row["reasoning_sha256"] == hashlib.sha256(thought.encode()).hexdigest()
    assert row["reasoning_characters"] == len(thought)
    assert row["visible_characters"] == len("画面是聊天界面")
    assert row["usage"]["completion_tokens_details"] == {"reasoning_tokens": 12}
    assert row["finish_reason"] == "stop"
    assert row["reasoning_first_nonwhitespace_ms"] is not None
    assert row["visible_first_nonwhitespace_ms"] is not None
    assert row["reasoning_text_saved"] is False
    assert thought not in json.dumps(row, ensure_ascii=False)
    assert "画面是聊天界面" not in json.dumps(row, ensure_ascii=False)


@pytest.mark.asyncio
async def test_normal_eof_reads_unterminated_tail_timings_and_keeps_unknown_null():
    raw = data({"choices": [], "timings": {"prompt_ms": 5, "draft_n": 0}}, b"")
    row = await observe([raw[:10], raw[10:]])
    assert row["native_timings"]["prompt_ms"] == 5
    assert row["native_timings"]["draft_n"] == 0
    assert row["native_timings"]["draft_n_accepted"] is None
    assert row["reasoning_tokens"] is None and row["visible_tokens"] is None
    assert row["done"] is False
    row = await observe([b"data: [DONE]"])
    assert row["done"] is True
    assert row["native_timings_seen"] is False
    assert all(value is None for value in row["native_timings"].values())


@pytest.mark.asyncio
async def test_malformed_event_choice_delta_and_utf8_do_not_break_stream():
    bad = [
        None,
        [],
        3,
        "text",
        {"choices": None},
        {"choices": {}},
        {"choices": [False, [], "bad", {"delta": []}, {"delta": 2}]},
    ]
    chunks = [data(event) for event in bad]
    chunks += [b"data: malformed-json\n\n", b"data: \xff\n\n"]
    chunks += [data({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})]
    row = await observe(chunks)
    assert row["observer_parse_errors"] == 13
    assert row["visible_characters"] == 2
    assert row["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_oversized_line_is_discarded_until_its_real_newline():
    chunks = [
        b"data: " + b"x" * 2_000_000,
        b"data: [DONE]",
        b"\n",
        data({"choices": [], "timings": {"draft_n": 0}}),
    ]
    row = await observe(chunks)
    assert row["observer_parse_errors"] == 1
    assert row["done"] is False
    assert row["native_timings"]["draft_n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [asyncio.CancelledError(), httpx.ReadError("fixture")])
async def test_cancel_or_transport_failure_does_not_complete_pending_line(error):
    records = []
    raw = data({"choices": [], "timings": {"prompt_ms": 9}}, b"")
    inner = Chunks([raw], error)
    transport = StageTapTransport(
        records,
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=inner, headers={"content-type": "text/event-stream"})
        ),
    )
    response = await transport.handle_async_request(httpx.Request("POST", "http://mock/chat/completions"))
    assert isinstance(response.stream, NativeStreamTap)
    with pytest.raises(type(error)):
        async for chunk in response.stream:
            assert chunk == raw
    assert records[0]["native_timings_seen"] is False
    assert records[0]["done"] is False
    await response.aclose()
    await response.aclose()
    await transport.aclose()
    assert inner.closes == 1


@pytest.mark.asyncio
async def test_early_close_does_not_parse_tail_or_extend_observed_duration():
    records = []
    clock = Clock()
    inner = Chunks([b"data: [DONE]"])
    transport = StageTapTransport(
        records,
        httpx.MockTransport(
            lambda request: httpx.Response(200, stream=inner, headers={"content-type": "text/event-stream"})
        ),
        clock=clock,
    )
    response = await transport.handle_async_request(httpx.Request("POST", "http://mock/chat/completions"))
    iterator = response.stream.__aiter__()
    assert await anext(iterator) == b"data: [DONE]"
    clock.value = 10.5
    await response.aclose()
    clock.value = 12
    await iterator.aclose()
    assert records[0]["done"] is False
    assert records[0]["observed_stream_end_ms"] == 500
    assert inner.closes == 1
    await transport.aclose()


@pytest.mark.asyncio
async def test_arrival_phases_are_separate_not_a_sum_of_overlapping_native_work():
    clock = Clock()
    records = []

    class TimedChunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for at, event in [
                (10.1, {"choices": [{"delta": {"reasoning_content": " "}}]}),
                (10.2, {"choices": [{"delta": {"reasoning_content": "想"}}]}),
                (10.3, {"choices": [{"delta": {"content": "答"}}]}),
                (10.4, {"timings": {"prompt_ms": 200, "predicted_ms": 200}}),
            ]:
                clock.value = at
                yield data(event)

    transport = StageTapTransport(
        records,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=TimedChunks(), headers={"content-type": "text/event-stream"}
            )
        ),
        clock=clock,
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post("http://mock/chat/completions")
    row = records[0]
    assert row["reasoning_first_nonwhitespace_ms"] == pytest.approx(200)
    assert row["visible_first_nonwhitespace_ms"] == pytest.approx(300)
    assert row["observed_stream_end_ms"] == pytest.approx(400)
    assert row["native_timings"]["prompt_ms"] == row["native_timings"]["predicted_ms"] == 200
    assert "do not add overlapping" in row["clock_semantics"]


@pytest.mark.asyncio
async def test_streaming_request_unconsumed_non_sse_and_other_routes_unchanged():
    records = []
    seen = []
    request_body = Chunks([b"secret-", b"body"])

    async def handler(request):
        seen.append((request, await request.aread()))
        return httpx.Response(200, content=b'{"plain":true}')

    transport = StageTapTransport(records, httpx.MockTransport(handler))
    request = httpx.Request(
        "POST",
        "http://mock/chat/completions",
        stream=request_body,
        headers={"authorization": "Bearer private-fixture"},
    )
    response = await transport.handle_async_request(request)
    assert response.content == b'{"plain":true}'
    assert seen == [(request, b"secret-body")]
    assert records[0]["wire_sha256"] is None
    assert records[0]["stream_not_sse"] is True
    assert "secret" not in json.dumps(records)
    assert "private-fixture" not in json.dumps(records)
    await transport.handle_async_request(httpx.Request("GET", "http://mock/health"))
    assert len(records) == 1 and len(seen) == 2
    await transport.aclose()


@pytest.mark.asyncio
async def test_transport_error_type_retained_without_error_text_or_retry():
    calls = []
    records = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private-error-detail")

    transport = StageTapTransport(records, httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectError):
        await transport.handle_async_request(httpx.Request("POST", "http://mock/chat/completions"))
    assert len(calls) == 1
    assert records[0]["transport_error_type"] == "ConnectError"
    assert "private-error-detail" not in json.dumps(records)
    await transport.aclose()
