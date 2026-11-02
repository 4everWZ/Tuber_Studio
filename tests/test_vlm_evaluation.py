"""Evaluation protocol/security boundaries. Network and model payloads are controlled here."""

import asyncio
import importlib.util
import json
import ssl
import struct
import zlib
from collections import deque
from pathlib import Path

import httpx
import pytest

_spec = importlib.util.spec_from_file_location(
    "vlm_operations", Path(__file__).resolve().parents[1] / "evaluations/vlm_operations.py"
)
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


async def public_resolver(host, port):
    return ["93.184.216.34"]


def final(text="鱼鱼觉得这很有意思。", channel="public", refs=None, citations=None):
    return json.dumps(
        {"text": text, "channel": channel, "citations": citations or [], "evidence_refs": refs or ["turn:0"]},
        ensure_ascii=False,
    )


def sse(delta, *, finish="stop", usage=None, done=True):
    rows = [
        {"model": "actual-candidate", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    if usage is not None:
        rows.append({"choices": [], "usage": usage})
    payload = "".join("data: " + json.dumps(row, ensure_ascii=False) + "\n\n" for row in rows)
    if done:
        payload += "data: [DONE]\n\n"
    return httpx.Response(200, content=payload.encode(), headers={"Content-Type": "text/event-stream"})


def web_fixture(handler, *, maximum=2 * 1024 * 1024, resolver=public_resolver):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    return v.PublicWeb(client=client, resolver=resolver, maximum=maximum), client


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "https://user:password@example.org/",
        "http://example.org/\r\nHost:localhost",
    ],
)
async def test_web_tools_deny_nonpublic_targets_before_requests(url):
    requests = []
    web, client = web_fixture(lambda request: requests.append(request) or httpx.Response(200))
    async with client:
        with pytest.raises(v.EvaluationError):
            await web.fetch(url)
    assert not requests


async def test_dns_is_pinned_and_host_tls_name_are_preserved():
    calls, requests = [], []

    async def rebinding(host, port):
        calls.append(host)
        return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

    def response(request):
        requests.append(request)
        return httpx.Response(
            200, text="<title>真实页面</title><p>已读取的公开内容</p>", headers={"Content-Type": "text/html"}
        )

    web, client = web_fixture(response, resolver=rebinding)
    async with client:
        document = await web.fetch("https://example.org/fact")
    assert calls == ["example.org"]
    assert requests[0].url.host == "93.184.216.34"
    assert requests[0].headers["host"] == "example.org"
    assert requests[0].extensions["sni_hostname"] == "example.org"
    assert document["url"] == "https://example.org/fact" and "已读取" in document["text"]


async def test_proxy_tls_wrapper_preserves_certificate_checks_for_original_hostname():
    seen = []

    class Stream:
        async def start_tls(self, context, hostname, timeout):
            seen.append((context.verify_mode, hostname))
            return self

    await v._SNIStream(Stream(), "93.184.216.34", "www.w3.org").start_tls(
        ssl.create_default_context(), "93.184.216.34"
    )
    assert seen == [(ssl.CERT_REQUIRED, "www.w3.org")]


async def test_redirect_to_private_ip_is_not_followed_and_downloads_are_bounded():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

    web, client = web_fixture(redirect)
    async with client:
        with pytest.raises(v.EvaluationError, match="globally routable"):
            await web.fetch("https://example.org/start")
    assert len(requests) == 1
    web, client = web_fixture(
        lambda request: httpx.Response(200, content=b"x" * 33, headers={"Content-Type": "text/plain"}),
        maximum=32,
    )
    async with client:
        with pytest.raises(v.EvaluationError, match="download limit"):
            await web.fetch("https://example.org/oversize")


async def test_actual_search_result_parsing_and_challenges_are_separate_outcomes():
    rss = b"<rss><channel><item><title>PNG specification</title><link>https://example.org/png</link><description>lossless</description></item></channel></rss>"
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(200, content=rss)

    web, client = web_fixture(response)
    async with client:
        result = await v.Search(web).run("PNG lossless")
    assert result["provider"] == "bing-rss"
    assert result["results"][0]["url"] == "https://example.org/png"
    assert requests[0].url.params["format"] == "rss" and requests[0].url.params["q"] == "PNG lossless"
    web, client = web_fixture(lambda request: httpx.Response(200, text="<html>captcha</html>"))
    async with client:
        with pytest.raises(v.EvaluationError) as failure:
            await v.Search(web).run("PNG")
    assert failure.value.kind == "search_no_results"


async def test_whitelist_private_context_and_secrets_cannot_escape_through_tools():
    requests = []
    secret = "EVALUATION_SECRET_DO_NOT_LOG"
    web, client = web_fixture(
        lambda request: (
            requests.append(request)
            or httpx.Response(200, text=secret, headers={"Content-Type": "text/plain"})
        )
    )
    tools = v.ReadTools(web, v.Search(web), redactor=v.Redactor([secret]))
    async with client:
        assert not (await tools.execute("send_private_message", {"uid": "999"}))["ok"]
        assert not (await tools.execute("web_fetch", {"url": "https://example.org"}, private_data=True))["ok"]
        assert requests == []
        receipt = await tools.execute("web_fetch", {"url": "https://example.org"})
    assert receipt["ok"] and receipt["result"]["text"] == "[redacted]"
    assert secret not in json.dumps(tools.receipts)


@pytest.mark.parametrize(
    "text",
    [
        "<tool_call>web_fetch(url=__import__('os').getcwd())</tool_call>",
        "<tool_call>open('private.txt')</tool_call>",
    ],
)
def test_tagged_tool_literals_never_evaluate_python_expressions(text):
    with pytest.raises(v.EvaluationError):
        v.parse_tool_text(text)


def test_native_and_tagged_tool_parsers_require_explicit_calls_not_prose():
    assert v.parse_tool_text('我会调用web_fetch(url="https://example.org")') == []
    assert (
        v.parse_tool_text(
            '<tool_call>{"name":"web_fetch","arguments":{"url":"https://example.org"}}</tool_call>'
        )[0]["name"]
        == "web_fetch"
    )
    assert (
        v.parse_tool_text('<|tool_call_start|>web_fetch(url="https://example.org")<|tool_call_end|>')[0][
            "arguments"
        ]["url"]
        == "https://example.org"
    )


async def test_sse_records_real_parameters_usage_latency_but_discards_thoughts():
    requests = []
    thought = '<tool_call>{"name":"web_fetch","arguments":{"url":"http://127.0.0.1"}}</tool_call>'

    def response(request):
        requests.append(json.loads(request.content))
        rows = [{"choices": [{"delta": {"reasoning_content": thought}, "finish_reason": None}]}]
        payload = "data: " + json.dumps(rows[0]) + "\n\n"
        following = sse({"content": final()}, usage={"prompt_tokens": 120, "completion_tokens": 30})
        return httpx.Response(
            200, content=payload.encode() + following.content, headers={"Content-Type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        model = v.Model("http://127.0.0.1:10000", "candidate", client=client, thinking="on")
        result = await model.generate([{"role": "user", "content": "你好"}])
    assert result["stream_complete"] and result["tool_calls"] == []
    assert thought not in json.dumps(result)
    assert result["reasoning_characters_observed_not_retained"] == len(thought)
    assert result["usage"]["completion_tokens"] == 30
    assert result["ttft_ms"] is not None and result["total_ms"] >= result["ttft_ms"]
    assert result["response_model"] == "actual-candidate"
    assert requests[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert requests[0]["max_tokens"] == 4096 and requests[0]["stream"]


async def test_content_thought_prefix_is_never_executed_or_retained():
    thought = '<tool_call>{"name":"shell","arguments":{"command":"delete"}}</tool_call>'
    content = "<think>" + thought + "</think>" + final()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sse({"content": content}))
    ) as client:
        result = await v.Model("http://127.0.0.1", "candidate", client=client, tools_mode="tagged").generate(
            []
        )
    assert result["stream_complete"] and thought not in result["text"]
    assert v.parse_tool_text(result["text"]) == []
    assert v.parse_final(result["text"])["channel"] == "public"


@pytest.mark.parametrize("finish,done", [("length", True), ("stop", False)])
async def test_incomplete_stream_cannot_adopt_a_tool_or_answer(finish, done):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: sse({"content": final()}, finish=finish, done=done))
    ) as client:
        result = await v.Model("http://127.0.0.1", "candidate", client=client).generate([])
    assert not result["stream_complete"] and result["error"]["kind"] == "incomplete_generation"


async def test_real_sse_tool_cycle_executes_only_whitelisted_reads_then_checks_quotes():
    model_requests, public_requests = [], []

    def model_response(request):
        body = json.loads(request.content)
        model_requests.append(body)
        if len(model_requests) == 1:
            return sse(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {
                                "name": "web_fetch",
                                "arguments": '{"url":"https://example.org/png"}',
                            },
                        }
                    ]
                },
                finish="tool_calls",
            )
        assert any(message["role"] == "tool" for message in body["messages"])
        return sse(
            {
                "content": final(
                    "PNG是无损格式，鱼鱼读到原文了。",
                    refs=["tool:1"],
                    citations=[{"url": "https://example.org/png", "quote": "PNG is lossless"}],
                )
            }
        )

    def public_response(request):
        public_requests.append(request)
        return httpx.Response(200, text="PNG is lossless", headers={"Content-Type": "text/plain"})

    web, web_client = web_fixture(public_response)
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(model_response)) as client:
        model = v.Model("http://127.0.0.1", "candidate", client=client)
        result = await v.run_case(
            {
                "id": "tools",
                "turns": [
                    {
                        "text": "实际读取PNG资料。",
                        "criteria": {"require_citations": True, "required_tools": ["web_fetch"]},
                    }
                ],
            },
            model,
            v.ReadTools(web, v.Search(web)),
            character={"name": "大肥鱼"},
            image_url="unused",
        )
    checks = result["turns"][0]["checks"]
    assert len(public_requests) == 1 and result["tool_calls"] == 1
    assert "tools" in model_requests[0] and model_requests[0]["tool_choice"] == "auto"
    assert result["execution_path"] == result["turns"][0]["execution_path"] == "tool_task"
    assert checks["evidence_validation"]["citation_quotes_grounded_in_fetched_text"]
    assert checks["protocol_delivery"]["structured_reply"]
    assert checks["semantic_quality_accepted"] is None and checks["requires_human_fact_review"]


async def test_model_never_executes_more_tools_after_budget_is_exhausted():
    def repeated(request):
        return sse(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "loop",
                        "function": {"name": "web_fetch", "arguments": '{"url":"https://example.org"}'},
                    }
                ]
            },
            finish="tool_calls",
        )

    web, web_client = web_fixture(
        lambda request: httpx.Response(200, text="public", headers={"Content-Type": "text/plain"})
    )
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(repeated)) as client:
        tools = v.ReadTools(web, v.Search(web))
        result = await v.run_case(
            {"id": "bounded", "turns": [{"text": "查证"}]},
            v.Model("http://127.0.0.1", "candidate", client=client),
            tools,
            character={},
            image_url="unused",
            max_tool_rounds=1,
        )
    assert len(tools.receipts) == 1 and result["model_calls"] == 2
    assert result["turns"][0]["error"]["kind"] == "tool_budget"


@pytest.mark.parametrize(
    "patch", [{"evidence_refs": []}, {"channel": "wrong"}, {"recipient_uid": "arbitrary"}]
)
def test_final_schema_does_not_accept_empty_evidence_or_extra_recipient(patch):
    value = json.loads(final())
    value.update(patch)
    with pytest.raises(v.EvaluationError):
        v.parse_final(json.dumps(value))


def test_structure_factual_facets_and_network_blocking_are_not_semantic_acceptance():
    value = v.parse_final(
        final(
            "PNG是无损格式。",
            refs=["tool:1"],
            citations=[{"url": "https://fabricated.example", "quote": "fake"}],
        )
    )
    receipts = [
        {
            "ok": False,
            "ref": "tool:1",
            "name": "web_fetch",
            "error": {"kind": "http_status", "status_code": 412},
        }
    ]
    checks = v.evaluate_answer(
        value, {"required_tools": ["web_fetch"], "require_citations": True}, receipts, {"turn:0"}, "public"
    )
    assert not checks["evidence_validation"]["evidence_refs_valid"]
    assert not checks["evidence_validation"]["citation_quotes_grounded_in_fetched_text"]
    assert checks["tool_environment"]["online_evidence_blocked"]
    assert checks["evidence_validation"]["ungrounded_citation_urls"]
    assert checks["semantic_quality_accepted"] is None


async def test_private_turn_content_is_not_copied_into_later_public_model_history():
    requests = []

    def response(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse({"content": final("鱼鱼会保护你的隐私。", channel="private")})
        assert "海盐73" not in json.dumps(body["messages"], ensure_ascii=False)
        return sse({"content": final("鱼鱼不会公开别人的隐私。", refs=["turn:1"])})

    web, web_client = web_fixture(lambda request: httpx.Response(200))
    corpus = json.loads(v.DEFAULT_CASES.read_text(encoding="utf-8"))
    case = next(item for item in corpus["cases"] if item["id"] == "private-context-no-public-leak")
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await v.run_case(
            case,
            v.Model("http://127.0.0.1", "candidate", client=client),
            v.ReadTools(web, v.Search(web)),
            character={},
            image_url="unused",
        )
    assert all(turn["checks"]["routing"]["matches_expected_channel"] for turn in result["turns"])
    assert all("海盐73" not in turn["answer"]["text"] for turn in result["turns"])


def test_png_fixture_contains_the_declared_independent_colored_components():
    image = v.make_png(128, 96, 2, 3)
    assert image[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", image[16:24]) == (128, 96)
    position, compressed = 8, b""
    while position < len(image):
        size = struct.unpack(">I", image[position : position + 4])[0]
        if image[position + 4 : position + 8] == b"IDAT":
            compressed += image[position + 8 : position + 8 + size]
        position += 12 + size
    raw = zlib.decompress(compressed)
    colors = {}
    for y in range(96):
        row = raw[y * (128 * 3 + 1) + 1 : (y + 1) * (128 * 3 + 1)]
        for x in range(128):
            color = tuple(row[x * 3 : x * 3 + 3])
            if color != (255, 255, 255):
                colors[(x, y)] = color
    components = {}
    while colors:
        origin, color = next(iter(colors.items()))
        pending = deque([origin])
        count = 0
        while pending:
            point = pending.popleft()
            if colors.get(point) != color:
                continue
            colors.pop(point)
            count += 1
            x, y = point
            pending.extend([(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)])
        assert count > 0
        components[color] = components.get(color, 0) + 1
    assert components == {(220, 35, 45): 2, (25, 75, 220): 3}


def test_custom_counts_update_criteria_without_leaking_answers_into_image_prompt():
    cases = json.loads(v.DEFAULT_CASES.read_text(encoding="utf-8"))["cases"]
    case = next(item for item in v.apply_case_parameters(cases, 4, 1) if item["id"] == "image-count-cn")
    good = v.evaluate_answer(
        v.parse_final(final("红色有四个圆形，蓝色有一个正方形。", refs=["fixture:shapes"])),
        case["turns"][0]["criteria"],
        [],
        {"fixture:shapes"},
        "public",
    )
    wrong = v.evaluate_answer(
        v.parse_final(final("红色有两个圆形，蓝色有三个正方形。", refs=["fixture:shapes"])),
        case["turns"][0]["criteria"],
        [],
        {"fixture:shapes"},
        "public",
    )
    assert all(good["literal_diagnostics"]["required_pattern_groups_found"])
    assert not all(wrong["literal_diagnostics"]["required_pattern_groups_found"])
    assert good["semantic_quality_accepted"] is None and wrong["semantic_quality_accepted"] is None
    assert "四" not in case["turns"][0]["text"]


@pytest.mark.parametrize(
    "reply, recalled, exact",
    [
        ("珊瑚-7319", True, True),
        ("珊瑚 -7319", True, False),
        ("珊瑚-\n7319", True, False),
        ("珊瑚-7318", False, False),
        ("珊瑚-73 19", False, False),
        ("珊瑚-73190", False, False),
        ("珊瑚-7319_extra", False, False),
    ],
)
async def test_context_capacity_full_usage_and_recall_are_independent(reply, recalled, exact):
    calls = []

    def response(request):
        body = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(body["messages"], ensure_ascii=False)})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * len(body["content"])})
        prompt_tokens = len(json.dumps(body["messages"], ensure_ascii=False))
        return sse({"content": reply}, usage={"prompt_tokens": prompt_tokens, "completion_tokens": 8})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await v.context_probe(
            v.Model("http://127.0.0.1", "candidate", client=client), 2000, 1000, 4096, 4096
        )
        assert result["actual_input_tokens"] <= 2000 and result["input_plus_reserved_output"] <= 4096
        assert result["capacity_admitted"]
        assert result["recall_passed"] is recalled and result["recall_exact_match"] is exact
        assert result["recall_match_method"] == "separator_whitespace_identifier_boundary"
        old = len(calls)
        rejected = await v.context_probe(
            v.Model("http://127.0.0.1", "candidate", client=client), 4096, 1000, 4096, 4096
        )
        assert rejected["error"]["kind"] == "probe_budget_exceeds_context" and len(calls) == old


async def test_missing_native_tokenizer_never_uses_character_estimates_for_capacity():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as client:
        result = await v.context_probe(
            v.Model("http://127.0.0.1", "candidate", client=client), 2000, 1000, 4096, None
        )
    assert result["error"]["kind"] == "http_status" and not result["capacity_admitted"]


def test_character_loading_uses_only_public_core_style_rules(tmp_path):
    path = tmp_path / "character.json"
    path.write_text(
        json.dumps({"id": "whale", "core": {}, "style": {}, "rules": [], "history": "PRIVATE_HISTORY"}),
        encoding="utf-8",
    )
    assert "PRIVATE_HISTORY" not in json.dumps(v.load_character(path))


def test_cli_keeps_network_proxy_and_context_reserves_explicit():
    args = v.arguments(
        [
            "--base-url",
            "http://127.0.0.1:18080",
            "--model",
            "candidate",
            "--quantization",
            "Q4_K_M",
            "--output",
            "unused.json",
            "--network-proxy",
            "http://127.0.0.1:7897",
            "--thinking",
            "on",
            "--context-probe-tokens",
            "120000",
        ]
    )
    assert args.context_tokens == 131072 and args.context_probe_output_tokens == 8192
    assert args.max_output_tokens == 4096 and args.network_proxy.endswith(":7897")


async def test_honest_network_failure_is_not_a_fabricated_citation_or_fact_verdict():
    answer = v.parse_final(final("网络访问失败，鱼鱼还没法核实这个说法。"))
    receipts = [
        {
            "ref": "tool:1",
            "name": "web_fetch",
            "ok": False,
            "error": {"kind": "http_status", "status_code": 412},
        }
    ]
    checks = v.evaluate_answer(
        answer, {"required_tools": ["web_fetch"], "require_citations": True}, receipts, {"turn:0"}, "public"
    )
    assert checks["tool_environment"]["online_evidence_blocked"]
    assert checks["evidence_validation"]["ungrounded_citation_urls"] == []
    assert checks["literal_diagnostics"]["pattern_occurrences"] == []
    assert checks["semantic_quality_accepted"] is None and checks["requires_human_fact_review"]


async def test_incomplete_native_tool_stream_cannot_reach_the_web_executor():
    requests = []
    call = {
        "index": 0,
        "id": "partial",
        "function": {"name": "web_fetch", "arguments": '{"url":"https://example.org"}'},
    }
    web, web_client = web_fixture(lambda request: requests.append(request) or httpx.Response(200))
    async with (
        web_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: sse({"tool_calls": [call]}, finish="length"))
        ) as client,
    ):
        tools = v.ReadTools(web, v.Search(web))
        result = await v.run_case(
            {"id": "partial", "turns": [{"text": "读取资料"}]},
            v.Model("http://127.0.0.1", "candidate", client=client),
            tools,
            character={},
            image_url="unused",
        )
    assert not requests and not tools.receipts
    assert result["turns"][0]["error"]["kind"] == "incomplete_generation"


async def test_optional_asset_images_are_ordered_and_do_not_replace_geometry():
    seen = []

    def response(request):
        seen.append(json.loads(request.content))
        return sse({"content": final("鱼鱼看到第一张是圆形、第二张是方形。", refs=["asset:1", "asset:2"])})

    web, web_client = web_fixture(lambda request: httpx.Response(200))
    images = [
        {
            "ref": "asset:1",
            "data_url": "data:image/png;base64,FIRST",
            "actual_time": 10.0,
            "requested_time": 11.25,
        },
        {"ref": "asset:2", "data_url": "data:image/png;base64,SECOND"},
    ]
    case = {"id": "assets", "requires_assets": True, "turns": [{"text": "描述静态素材", "image": "assets"}]}
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await v.run_case(
            case,
            v.Model("http://127.0.0.1", "candidate", client=client),
            v.ReadTools(web, v.Search(web)),
            character={},
            image_url="GEOMETRY_UNCHANGED",
            asset_images=images,
        )
    content = seen[0]["messages"][-1]["content"]
    assert [part["image_url"]["url"] for part in content if part["type"] == "image_url"] == [
        image["data_url"] for image in images
    ]
    evidence = json.loads(content[0]["text"].split("：", 1)[1].split("\n", 1)[0])["image_evidence"]
    assert evidence[0] == {"ref": "asset:1", "actual_time": 10.0}
    assert "requested_time" not in json.dumps(seen, ensure_ascii=False)
    assert images[0]["requested_time"] == 11.25
    assert result["turns"][0]["checks"]["evidence_validation"]["required_evidence_present"]


async def test_capacity_does_not_pass_just_because_the_model_recalls_anchor():
    def response(request):
        body = json.loads(request.content)
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": json.dumps(body["messages"], ensure_ascii=False)})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1] * len(body["content"])})
        count = len(json.dumps(body["messages"], ensure_ascii=False))
        return sse({"content": "珊瑚-7319"}, usage={"prompt_tokens": count - 1, "completion_tokens": 8})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await v.context_probe(
            v.Model("http://127.0.0.1", "candidate", client=client), 2000, 1000, 4096, 4096
        )
    assert result["recall_passed"] and not result["full_input_confirmed"]
    assert not result["capacity_admitted"]


async def test_complete_report_records_actual_runtime_fixture_and_declared_quantization(
    tmp_path, monkeypatch
):
    requests = []

    def response(request):
        requests.append(request)
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"model_alias": "candidate", "default_generation_settings": {"n_ctx": 131072}}
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "candidate"}]})
        return sse(
            {"content": final("鱼鱼看到红色有两个圆形、蓝色有三个正方形。", refs=["fixture:shapes"])},
            usage={"prompt_tokens": 100, "completion_tokens": 30},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        original = v.Model
        monkeypatch.setattr(v, "Model", lambda *args, **kwargs: original(*args, **kwargs, client=client))
        args = v.arguments(
            [
                "--base-url",
                "http://127.0.0.1",
                "--model",
                "candidate",
                "--quantization",
                "Q4_K_M",
                "--case-id",
                "image-count-cn",
                "--output",
                str(tmp_path / "report.json"),
            ]
        )
        report = await v.evaluate(args)
    saved = json.loads(args.output.read_text(encoding="utf-8"))
    assert report["context_configuration_verified"] and saved["declarations"]["quantization"] == "Q4_K_M"
    assert len(saved["cases"]) == 1 and saved["cases"][0]["turns"][0]["structural_complete"]
    assert args.output.with_suffix(".fixture.png").read_bytes().startswith(b"\x89PNG")
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert saved["scope"]["platform_write_tools_available"] is False
    assert saved["declarations"]["image_preprocessing"] == "server_native"
    image_record = saved["materials"]["image"]
    native_input = image_record["model_input"]
    assert native_input["profile"] == "server_native"
    assert native_input["parent_sha256"] == native_input["derived_sha256"] == image_record["sha256"]
    wire_image = next(
        part
        for part in json.loads(requests[-1].content)["messages"][-1]["content"]
        if part["type"] == "image_url"
    )
    assert (
        v.base64.b64decode(wire_image["image_url"]["url"].split(",", 1)[1])
        == args.output.with_suffix(".fixture.png").read_bytes()
    )
    assert Path(native_input["path"]).resolve() == args.output.with_suffix(".fixture.png").resolve()
    assert not (args.output.parent / (args.output.stem + ".images")).exists()
    assert saved["protocol"] == "vlm-operations-v4-oc-paths"
    assert saved["group_summary"]["core"]["cases_recorded"] == 1
    assert saved["group_summary"]["safety_diagnostic"]["cases_recorded"] == 0
    assert saved["group_summary"]["core"]["semantic_quality_accepted"] is None
    payload = json.loads(requests[-1].content)
    assert (
        saved["materials"]["tool_task_system_prompt_sha256"]
        == v.hashlib.sha256(payload["messages"][0]["content"].encode()).hexdigest()
    )


async def test_explicit_runtime_context_mismatch_blocks_evaluation_instead_of_silently_shrinking(
    tmp_path, monkeypatch
):
    requests = []

    def response(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"model_alias": "candidate", "default_generation_settings": {"n_ctx": 8192}}
            if request.url.path == "/props"
            else {"data": [{"id": "candidate"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        original = v.Model
        monkeypatch.setattr(v, "Model", lambda *args, **kwargs: original(*args, **kwargs, client=client))
        args = v.arguments(
            [
                "--base-url",
                "http://127.0.0.1",
                "--model",
                "candidate",
                "--quantization",
                "Q4_K_M",
                "--case-id",
                "image-count-cn",
                "--output",
                str(tmp_path / "report.json"),
            ]
        )
        report = await v.evaluate(args)
    assert report["runtime_configuration_error"] == "runtime_context_mismatch"
    assert report["cases"][0]["blocked"] == "runtime_context_mismatch"
    assert all(request.method == "GET" for request in requests)


async def test_preflight_is_persisted_without_any_model_request_or_model_score(tmp_path, monkeypatch):
    model_requests = []

    def model_response(request):
        model_requests.append(request)
        raise AssertionError("Tool preflight must not contact a model")

    def public_response(request):
        if request.url.path == "/search":
            return httpx.Response(
                200,
                text="<rss><channel><item><title>PNG</title><link>https://www.w3.org/TR/png-3/</link><description>PNG</description></item></channel></rss>",
            )
        return httpx.Response(200, text="PNG is lossless", headers={"Content-Type": "text/plain"})

    web, web_client = web_fixture(public_response)
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(model_response)) as client:
        original_model = v.Model
        monkeypatch.setattr(
            v, "Model", lambda *args, **kwargs: original_model(*args, **kwargs, client=client)
        )
        monkeypatch.setattr(v, "PublicWeb", lambda **kwargs: web)
        args = v.arguments(
            [
                "--base-url",
                "http://127.0.0.1",
                "--model",
                "not-loaded",
                "--quantization",
                "not-loaded",
                "--preflight-only",
                "--output",
                str(tmp_path / "preflight.json"),
            ]
        )
        report = await v.evaluate(args)
    saved = json.loads(args.output.read_text(encoding="utf-8"))
    assert saved["cases"] == [] and model_requests == []
    assert report["tool_preflight"]["model_requests_made"] == 0
    assert report["tool_preflight"]["source"] == "harness_only_not_model_tool_selection"
    assert all(receipt["ok"] for receipt in report["tool_preflight"]["receipts"])
    assert "observed_runtime" not in report


async def test_private_handoff_reply_never_returns_to_public_history():
    requests = []

    def response(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse({"content": final("私下的回复标记PRIVATE_HANDOFF_BODY。", channel="private")})
        assert "PRIVATE_HANDOFF_BODY" not in json.dumps(body["messages"], ensure_ascii=False)
        return sse({"content": final("鱼鱼已经选择私下继续这件事。", refs=["turn:1"])})

    web, web_client = web_fixture(lambda request: httpx.Response(200))
    case = {
        "id": "handoff",
        "private_data": True,
        "turns": [
            {"text": "请私下继续。", "expected_channel": "private"},
            {"text": "公开区只标记移交就好。"},
        ],
    }
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await v.run_case(
            case,
            v.Model("http://127.0.0.1", "candidate", client=client),
            v.ReadTools(web, v.Search(web)),
            character={},
            image_url="unused",
        )
    assert len(result["turns"]) == 2


async def test_model_http_error_records_failure_timing_without_echoing_secret_body():
    secret = "PRIVATE_MODEL_AUTH_TOKEN"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text=secret))
    ) as client:
        result = await v.Model("http://127.0.0.1", "candidate", client=client, key=secret).generate([])
    assert result["error"] == {"kind": "http_status", "status_code": 401}
    assert result["total_ms"] >= 0 and secret not in json.dumps(result)


def test_manifest_preserves_actual_frame_times_hashes_and_relative_sources(tmp_path):
    raw = v.make_png(128, 96)
    (tmp_path / "frame.png").write_bytes(raw)
    entry = {
        "id": "platform-frame",
        "cid": "837470340",
        "bvid": "BV1We4y1k7za",
        "file": "frame.png",
        "actual_time": 10.0,
        "requested_time": 11.25,
        "width": 128,
        "height": 96,
        "mime_type": "image/png",
        "bytes": len(raw),
        "image_sha256": v.hashlib.sha256(raw).hexdigest(),
    }
    manifest = tmp_path / "storyboard.json"
    manifest.write_text(json.dumps({"bvid": "BV1We4y1k7za", "frames": [entry]}), encoding="utf-8")
    images, records = v.load_asset_images(manifest_path=manifest)
    assert images[0]["ref"] == "frame:1"
    assert images[0]["actual_time"] == 10.0 and images[0]["requested_time"] == 11.25
    assert records[0]["platform_frame_id"] == "platform-frame"
    assert "path" not in images[0] and records[0]["sha256"] == entry["image_sha256"]
    entry["image_sha256"] = "0" * 64
    manifest.write_text(json.dumps({"bvid": "BV1We4y1k7za", "frames": [entry]}), encoding="utf-8")
    with pytest.raises(v.EvaluationError, match="differ from manifest"):
        v.load_asset_images(manifest_path=manifest)


def test_manifest_paths_cannot_escape_its_directory_and_jpeg_dimensions_are_read(tmp_path):
    jpeg_header = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x20\x00\x30\x01\x01\x11\x00\xff\xd9"
    assert v.image_details(jpeg_header) == ("image/jpeg", 48, 32)
    manifest = tmp_path / "storyboard.json"
    manifest.write_text(
        json.dumps({"bvid": "BV1We4y1k7za", "frames": [{"file": "../outside.png"}]}), encoding="utf-8"
    )
    with pytest.raises(v.EvaluationError, match="escapes"):
        v.load_asset_images(manifest_path=manifest)


def test_wrong_bilibili_video_is_not_counted_as_correct_source():
    checks = v.evaluate_answer(
        v.parse_final(final()),
        {"expected_bvid": "BV1We4y1k7za"},
        [
            {
                "name": "bilibili_read",
                "arguments": {"bvid": "BV1ms4y1J7fJ"},
                "ok": False,
                "ref": "tool:1",
                "error": {"kind": "restricted"},
            }
        ],
        {"turn:0"},
        "public",
    )
    assert checks["tool_environment"]["bilibili_target_matches"] is False
    assert checks["semantic_quality_accepted"] is None


@pytest.mark.parametrize("body", [{"max_completion_tokens": 999999}, {"n": 100}, {"api_key": "secret"}])
def test_extra_parameters_cannot_bypass_budgets_or_supply_credentials(body):
    with pytest.raises(v.EvaluationError, match="Extra parameters"):
        v.Model("http://127.0.0.1", "candidate", extra_body=body)


def test_invalid_model_tool_arguments_are_not_misreported_as_network_blocking():
    checks = v.evaluate_answer(
        v.parse_final(final()),
        {"required_tools": ["web_fetch"]},
        [{"name": "web_fetch", "ref": "tool:1", "ok": False, "error": {"kind": "invalid_arguments"}}],
        {"turn:0"},
        "public",
    )
    assert not checks["tool_environment"]["online_evidence_blocked"]
    assert checks["tool_environment"]["model_tool_request_errors"] == [
        {"ref": "tool:1", "kind": "invalid_arguments"}
    ]


async def run_repair_case(handler, *, format_repair=True, case=None, timeout=5):
    web, web_client = web_fixture(
        lambda request: httpx.Response(200, text="PNG is lossless.", headers={"Content-Type": "text/plain"})
    )
    async with web_client, httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tools = v.ReadTools(web, v.Search(web))
        result = await v.run_case(
            case
            or {
                "id": "format-pair",
                "turns": [
                    {
                        "text": "描述附图",
                        "image": "fixture:shapes",
                        "criteria": {"required_refs": ["fixture:shapes"]},
                    }
                ],
            },
            v.Model("http://127.0.0.1", "candidate", client=client, thinking="on"),
            tools,
            character={},
            image_url="data:image/png;base64,unchanged-fixture",
            format_repair=format_repair,
            timeout=timeout,
        )
    return result, tools.receipts


async def test_format_repair_is_explicit_paired_and_keeps_original_answer_failure_and_budgets():
    original = " 鱼鱼看到红色圆形有2个，蓝色正方形有3个。\n"
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse(
                {"content": original},
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            )
        assert "tools" not in body and "tool_choice" not in body
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == v.FINAL_SCHEMA
        assert "image_url" not in json.dumps(body["messages"])
        assert json.loads(body["messages"][1]["content"])["original_reply_text"] == original
        assert body["max_tokens"] == requests[0]["max_tokens"] == 4096
        assert body["chat_template_kwargs"] == requests[0]["chat_template_kwargs"]
        return sse(
            {"content": final(original, refs=["fixture:shapes"])},
            usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
        )

    result, receipts = await run_repair_case(handler)
    turn = result["turns"][0]
    assert not turn["baseline_structural_complete"] and not turn["structural_complete"]
    assert turn["answer"] is None and turn["error"]["kind"] == "invalid_final_json"
    assert turn["model_steps"][0]["text"] == original
    assert turn["format_repair_used"] and turn["repair_result"]["format_only_accepted"]
    assert turn["repair_result"]["answer"]["text"] == original
    assert turn["repair_result"]["checks"]["protocol_delivery"]["structured_reply"]
    assert turn["semantic_review"]["status"] == "pending"
    assert turn["repair_result"]["establishes_baseline_channel_or_citation_correctness"] is False
    assert result["model_calls"] == result["generation_calls"] == turn["generation_calls"] == 2
    assert result["usage"] == {"prompt_tokens": 150, "completion_tokens": 50, "total_tokens": 200}
    assert result["generation_total_ms"] == sum(s["total_ms"] for s in turn["model_steps"])
    assert not receipts


async def test_format_repair_defaults_off_and_never_retries_a_second_invalid_repair():
    requests = []

    def handler(request):
        requests.append(request)
        return sse({"content": "这是原来的自然回答。"})

    baseline, _ = await run_repair_case(handler, format_repair=False)
    assert len(requests) == 1 and not baseline["turns"][0]["format_repair_used"]
    requests.clear()
    paired, _ = await run_repair_case(handler)
    assert len(requests) == 2 and paired["turns"][0]["repair_result"]["error"]["kind"] == "invalid_final_json"


@pytest.mark.parametrize("repair_kind", ["native_tool", "tagged_tool", "length", "changed_fact"])
async def test_format_repair_refuses_tools_incomplete_stream_and_changed_original_facts(repair_kind):
    requests = []
    original = "鱼鱼看到九个红圆。"

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return sse({"content": original})
        if repair_kind == "native_tool":
            return sse(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "forbidden",
                            "function": {"name": "web_fetch", "arguments": '{"url":"https://example.org"}'},
                        }
                    ]
                },
                finish="tool_calls",
            )
        if repair_kind == "tagged_tool":
            return sse(
                {
                    "content": '<tool_call>{"name":"web_fetch","arguments":{"url":"https://example.org"}}</tool_call>'
                }
            )
        if repair_kind == "length":
            return sse({"content": final(original)}, finish="length")
        return sse({"content": final("鱼鱼看到两个红圆。")})

    result, receipts = await run_repair_case(handler)
    turn = result["turns"][0]
    assert turn["model_steps"][0]["text"] == original
    assert not turn["repair_result"]["format_only_accepted"]
    assert not receipts and len(requests) == 2
    expected = {
        "native_tool": "format_repair_tool_attempt",
        "tagged_tool": "format_repair_tool_attempt",
        "length": "incomplete_generation",
        "changed_fact": "format_repair_content_changed",
    }
    assert turn["repair_result"]["error"]["kind"] == expected[repair_kind]


async def test_length_baseline_is_never_eligible_for_format_repair():
    result, _ = await run_repair_case(lambda request: sse({"content": "未完成的自然答复"}, finish="length"))
    assert result["generation_calls"] == 1 and not result["turns"][0]["format_repair_used"]


async def test_format_repair_still_checks_citations_against_actual_fetched_materials():
    calls = []
    original = "PNG是无损格式。"

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return sse(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "actual-read",
                            "function": {
                                "name": "web_fetch",
                                "arguments": '{"url":"https://example.org/png"}',
                            },
                        }
                    ]
                },
                finish="tool_calls",
            )
        if len(calls) == 2:
            return sse({"content": original})
        return sse(
            {
                "content": final(
                    original,
                    refs=["tool:1"],
                    citations=[{"url": "https://example.org/png", "quote": "This phrase was never fetched"}],
                )
            }
        )

    result, receipts = await run_repair_case(
        handler,
        case={
            "id": "real-citation",
            "turns": [
                {"text": "联网查证", "criteria": {"required_tools": ["web_fetch"], "require_citations": True}}
            ],
        },
    )
    repair = result["turns"][0]["repair_result"]
    assert repair["content_preserved"] and repair["structural_complete"]
    assert repair["checks"]["evidence_validation"]["citation_quotes_grounded_in_fetched_text"] is False
    assert len(receipts) == 1 and receipts[0]["ok"]
    assert result["generation_calls"] == 3


async def test_format_repair_preserves_baseline_when_original_case_deadline_expires():
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return sse({"content": "鱼鱼原始回答。"}, usage={"completion_tokens": 5})
        await asyncio.sleep(10)
        raise AssertionError("The original deadline should cancel this request")

    result, receipts = await run_repair_case(handler, timeout=0.05)
    turn = result["turns"][0]
    assert result["deadline_expired"] and result["generation_calls"] == 2
    assert turn["model_steps"][0]["text"] == "鱼鱼原始回答。"
    assert turn["error"]["kind"] == "invalid_final_json"
    assert turn["repair_result"]["error"]["kind"] == "timeout"
    assert len(calls) == 2 and not receipts


async def test_paired_repair_is_not_written_into_later_public_or_private_history():
    requests = []
    original = "鱼鱼会私下陪你说。"

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return sse({"content": original})
        if len(requests) == 2:
            return sse({"content": final(original, channel="private")})
        assert original not in json.dumps(body["messages"], ensure_ascii=False)
        assert "original_reply_text" not in json.dumps(body["messages"])
        return sse({"content": final("鱼鱼继续公开聊。", refs=["turn:1"])})

    case = {
        "id": "two-turn-pair",
        "turns": [{"text": "请私下回应", "expected_channel": "private"}, {"text": "下一条公开问题"}],
    }
    result, _ = await run_repair_case(handler, case=case)
    assert len(requests) == 3 and not result["turns"][1]["format_repair_used"]
    assert result["turns"][0]["repair_result"]["written_to_followup_history"] is False


async def test_paired_repair_cannot_expand_the_existing_case_generation_limit():
    calls = []

    def handler(request):
        calls.append(request)
        return sse({"content": "鱼鱼原始回答。" if len(calls) % 2 else final("鱼鱼原始回答。")})

    result, _ = await run_repair_case(
        handler, case={"id": "bounded-multiturn", "turns": [{"text": "继续"} for _ in range(13)]}
    )
    assert len(calls) == result["generation_calls"] == result["model_calls"] == 24
    assert result["turns"][-1]["error"]["kind"] == "model_call_budget"
    assert not result["turns"][-1]["format_repair_used"]


def test_format_repair_preserves_decodable_text_without_copying_malformed_wrapper():
    assert v.format_repair_text('{"text":"原事实是九个。", "channel":public}') == "原事实是九个。"


async def test_format_repair_never_reanswers_a_structurally_valid_factual_failure():
    calls = []

    def handler(request):
        calls.append(request)
        return sse({"content": final("红圆有九个。")})

    result, _ = await run_repair_case(
        handler,
        case={
            "id": "wrong-fact",
            "turns": [{"text": "两个红圆", "criteria": {"required_regex_any": [["两个"]]}}],
        },
    )
    turn = result["turns"][0]
    assert turn["baseline_structural_complete"]
    assert turn["checks"]["literal_diagnostics"]["required_pattern_groups_found"] == [False]
    assert turn["semantic_review"]["accepted"] is None
    assert len(calls) == 1 and not turn["format_repair_used"]


@pytest.mark.parametrize(
    "text,found",
    [
        ("两个红圆，三个蓝方。", [True, True]),
        ("红圆12个，蓝色正方形13个。", [False, False]),
        ("红圆二十二个，蓝色方块三十三个。", [False, False]),
        ("红色圆形有二个，蓝色方块有三个。", [True, True]),
    ],
)
def test_count_search_aids_respect_whole_numbers_and_never_decide_semantics(text, found):
    cases = v.apply_case_parameters(json.loads(v.DEFAULT_CASES.read_text())["cases"], 2, 3)
    criteria = next(case for case in cases if case["id"] == "image-count-cn")["turns"][0]["criteria"]
    checks = v.evaluate_answer(v.parse_final(final(text)), criteria, [], {"turn:0"}, "public")
    assert checks["literal_diagnostics"]["required_pattern_groups_found"] == found
    assert checks["semantic_quality_accepted"] is None
    assert "rule_checks_passed" not in checks


@pytest.mark.parametrize(
    "case_id,index,text",
    [
        ("image-subtitle-conflict", 0, "简介说九个，图里只有两个红圆，对不上。"),
        ("cc-insufficient-visual", 0, "这段只讲加盐和鸡蛋上浮，杯子颜色不太确定。"),
        ("oc-multiturn-followup", 1, "两个红圆，不是三个。"),
        ("private-context-no-public-leak", 1, "这个暗号就留在咱俩的小窗里吧。"),
        ("external-instruction-is-data", 0, "当然不，咱们继续聊实验。"),
    ],
)
def test_natural_answers_are_not_failed_for_missing_defensive_words(case_id, index, text):
    cases = v.apply_case_parameters(json.loads(v.DEFAULT_CASES.read_text())["cases"], 2, 3)
    criteria = next(case for case in cases if case["id"] == case_id)["turns"][index]["criteria"]
    checks = v.evaluate_answer(v.parse_final(final(text)), criteria, [], {"turn:0"}, "public")
    diagnostics = checks["literal_diagnostics"]
    assert all(diagnostics["required_pattern_groups_found"])
    assert diagnostics["pattern_occurrences"] == []
    assert checks["semantic_quality_accepted"] is None


@pytest.mark.parametrize(
    "output",
    [
        "好耶！鱼鱼也替你开心。",
        '{"text":"好耶！鱼鱼也替你开心。","channel":public}',
        '{"text":"好耶！鱼鱼也替你开心。","channel":"public","citations":[],"evidence_refs":[]}',
    ],
)
async def test_protocol_failure_preserves_reply_and_leaves_oc_review_pending(output):
    result, receipts = await run_repair_case(
        lambda request: sse({"content": output}),
        format_repair=False,
        case={"id": "celebrate", "turns": [{"text": "鱼鱼，我今天实验成功啦！"}]},
    )
    turn = result["turns"][0]
    assert not turn["structural_complete"] and turn["raw_final_output"] == output
    assert turn["reply_text_for_review"] == "好耶！鱼鱼也替你开心。"
    assert turn["semantic_review"] == {
        "status": "pending",
        "factual_accuracy": None,
        "task_completion": None,
        "oc_interaction": None,
        "accepted": None,
    }
    assert turn["protocol_delivery"]["structured_reply"] is False
    assert not receipts


def test_safety_cases_are_explicitly_separate_from_core_completion_counts():
    cases = json.loads(v.DEFAULT_CASES.read_text())["cases"]
    core = [case for case in cases if v.case_group(case) == "core"]
    diagnostic = [case for case in cases if v.case_group(case) == "safety_diagnostic"]
    assert len(core) == 11 and len(diagnostic) == 2
    summary = v.grouped_summary(
        [
            {
                "id": "normal",
                "evaluation_group": "core",
                "turns": [{"structural_complete": True, "reply_text_for_review": "开心！"}],
            },
            {
                "id": "diagnostic",
                "evaluation_group": "safety_diagnostic",
                "blocked": "http_status",
                "turns": [],
            },
        ]
    )
    assert summary["core"]["cases_recorded"] == summary["core"]["paths"]["tool_task"]["structured_turns"] == 1
    assert summary["core"]["cases_blocked_or_failed"] == 0
    assert summary["safety_diagnostic"]["cases_blocked_or_failed"] == 1
    assert summary["core"]["semantic_quality_accepted"] is None


async def test_case_deadline_keeps_prior_raw_reply_for_review():
    calls = []

    async def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return sse({"content": "鱼鱼记住两个红圆啦。"})
        await asyncio.sleep(10)
        raise AssertionError("deadline should cancel generation")

    result, _ = await run_repair_case(
        handler,
        format_repair=False,
        timeout=0.05,
        case={"id": "memory", "turns": [{"text": "两个红圆"}, {"text": "几个呀？"}]},
    )
    assert result["deadline_expired"] and len(result["turns"]) == 2
    assert result["turns"][0]["raw_final_output"] == "鱼鱼记住两个红圆啦。"
    assert result["turns"][1]["error"]["kind"] == "timeout"
    assert result["turns"][1]["semantic_review"]["accepted"] is None


async def test_interrupted_stream_keeps_visible_text_without_retaining_thoughts():
    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            payload = {
                "choices": [
                    {"delta": {"reasoning_content": "PRIVATE_THOUGHT", "content": "鱼鱼看到两个红圆"}}
                ]
            }
            yield ("data: " + json.dumps(payload) + "\n\n").encode()
            raise httpx.ReadError("controlled interruption")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=Interrupted(), headers={"Content-Type": "text/event-stream"}
            )
        )
    ) as client:
        response = await v.Model("http://model", "candidate", client=client).generate([])
    assert response["text"] == "鱼鱼看到两个红圆" and not response["stream_complete"]
    assert response["error"]["kind"] == "ReadError"
    assert "PRIVATE_THOUGHT" not in json.dumps(response)


@pytest.mark.parametrize("first", ["先去旧沉船！鱼鱼把小水母举高一点，咱俩贴着海带林走。", "走钟楼这边！"])
async def test_oc_story_uses_natural_dialogue_without_tools_or_schema_and_keeps_history(first):
    cases = json.loads(v.DEFAULT_CASES.read_text())["cases"]
    case = next(case for case in cases if case["id"] == "oc-undersea-postal-adventure")
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        assert "tools" not in payload and "tool_choice" not in payload and "response_format" not in payload
        assert payload["messages"][-1]["content"] == case["turns"][len(seen) - 1]["text"]
        if len(seen) == 2:
            previous = [
                message["content"] for message in payload["messages"] if message["role"] == "assistant"
            ]
            assert previous == [first]
        text = first if len(seen) == 1 else "嗯，抓紧啦！"
        return sse({"content": text})

    result, _ = await run_repair_case(handler, format_repair=True, case=case)
    assert len(seen) == 2
    background = seen[0]["messages"][0]["content"]
    assert all(value in background for value in case["conversation_context"].values())
    assert all(turn["history_delivery"] == "plain_oc_reply" for turn in result["turns"])
    assert all(turn["structural_complete"] is None for turn in result["turns"])
    assert all(turn["protocol_delivery"]["complete"] for turn in result["turns"])
    assert all(turn["literal_diagnostics"] is None for turn in result["turns"])
    assert all(not turn["format_repair_used"] for turn in result["turns"])
    assert result["tool_calls"] == 0 and result["execution_path"] == "plain_oc"
    assert result["turns"][1]["answer"]["text"] == "嗯，抓紧啦！"
    for index, turn in enumerate(result["turns"]):
        assert turn["answer_field_sources"]["channel"] == "backend_scene_binding"
        assert turn["backend_binding"]["source_ref"] == f"turn:{index}"
        assert turn["backend_binding"]["model_decided_fields"] == []
        assert turn["checks"]["routing"]["assessed"] is False
        assert turn["tool_environment"]["tool_selection_assessed"] is False
    assert all(turn["semantic_review"]["accepted"] is None for turn in result["turns"])
    assert result["task_kind"] == "oc_conversation"


@pytest.mark.parametrize("boundary", ["private", "handoff", "diagnostic", "tools", "image"])
async def test_plain_oc_path_rejects_mixed_task_scopes_before_generation(boundary):
    case = {"id": "ordinary", "task_kind": "oc_conversation", "turns": [{"text": "接着聊吧"}]}
    if boundary == "private":
        case["private_data"] = True
    elif boundary == "handoff":
        case["turns"][0]["expected_channel"] = "private"
    elif boundary == "diagnostic":
        case["evaluation_group"] = "safety_diagnostic"
    elif boundary == "tools":
        case["turns"][0]["criteria"] = {"required_tools": ["web_search"]}
    else:
        case["turns"][0]["image"] = "fixture:shapes"
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: calls.append(request) or sse({"content": "台词"}))
    ) as client:
        with pytest.raises(v.EvaluationError, match="ordinary public dialogue"):
            await v.run_oc_conversation(
                case, v.Model("http://model", "candidate", client=client), character={}, timeout=5
            )
    assert calls == []


@pytest.mark.parametrize("failure", ["length", "tool_calls", "transport"])
async def test_plain_oc_records_abnormal_reply_without_execution_or_history_adoption(failure):
    requests = []
    prefix = "鱼鱼伸手接住小水母"

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": prefix}}]}) + "\n\n").encode()
            raise httpx.ReadError("controlled interruption")

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert "tools" not in body and "response_format" not in body
        if len(requests) == 2:
            assert not any(message["role"] == "assistant" for message in body["messages"])
            return sse({"content": "接着走吧。"})
        if failure == "transport":
            return httpx.Response(200, stream=Interrupted(), headers={"Content-Type": "text/event-stream"})
        if failure == "length":
            return sse({"content": prefix}, finish="length")
        return sse(
            {
                "content": prefix,
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "unexpected",
                        "function": {"name": "web_search", "arguments": '{"query":"珊瑚钟楼"}'},
                    }
                ],
            },
            finish="tool_calls",
        )

    result, receipts = await run_repair_case(
        handler,
        case={
            "id": "scene",
            "task_kind": "oc_conversation",
            "turns": [{"text": "走哪边？"}, {"text": "然后呢？"}],
        },
    )
    first = result["turns"][0]
    assert first["raw_final_output"] == first["reply_text_for_review"] == prefix
    assert first["answer"] is None and first["history_delivery"] == "none"
    assert first["protocol_delivery"]["complete"] is False
    assert first["semantic_review"]["accepted"] is None
    assert not receipts and result["tool_calls"] == 0
    assert len(requests) == 2 and result["turns"][1]["protocol_delivery"]["complete"]


async def test_plain_oc_complete_report_records_prompt_and_separate_path_counts(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"model_alias": "candidate", "default_generation_settings": {"n_ctx": 131072}}
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "candidate"}]})
        return sse({"content": "海底御膳白玉饭！鱼鱼批准午休摸鱼啦。"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        original = v.Model
        monkeypatch.setattr(v, "Model", lambda *a, **kw: original(*a, **kw, client=client))
        args = v.arguments(
            [
                "--base-url",
                "http://model",
                "--model",
                "candidate",
                "--quantization",
                "Q4",
                "--case-id",
                "oc-rice-and-slacking",
                "--format-repair",
                "--output",
                str(tmp_path / "oc.json"),
            ]
        )
        report = await v.evaluate(args)
    saved = json.loads(args.output.read_text())
    assert saved["protocol"] == "vlm-operations-v4-oc-paths"
    paths = saved["group_summary"]["core"]["paths"]
    assert paths["plain_oc"]["complete_deliveries"] == paths["plain_oc"]["turns_recorded"] == 1
    assert paths["plain_oc"]["structured_turns"] is None
    assert paths["tool_task"]["turns_recorded"] == 0
    body = json.loads(requests[-1].content)
    assert "tools" not in body and "response_format" not in body
    assert (
        saved["materials"]["oc_system_prompt_sha256_by_case"]["oc-rice-and-slacking"]
        == v.hashlib.sha256(body["messages"][0]["content"].encode()).hexdigest()
    )
    assert report["cases"][0]["turns"][0]["answer_field_sources"]["evidence_refs"] == "backend_scene_binding"
    assert len(requests) == 3


async def test_gemma_fixture_profile_sends_derived_bytes_and_preserves_parent_and_question(
    tmp_path, monkeypatch
):
    pytest.importorskip("PIL")
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"model_alias": "candidate", "default_generation_settings": {"n_ctx": 131072}}
            )
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "candidate"}]})
        return sse({"content": final("红圆两个，蓝方三个。", refs=["fixture:shapes"])})

    reports = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        original = v.Model
        monkeypatch.setattr(v, "Model", lambda *a, **kw: original(*a, **kw, client=client))
        for profile in ("server_native", "gemma4_image_280"):
            args = v.arguments(
                [
                    "--base-url",
                    "http://model",
                    "--model",
                    "candidate",
                    "--quantization",
                    "Q4",
                    "--case-id",
                    "image-count-cn",
                    "--image-preprocessing",
                    profile,
                    "--output",
                    str(tmp_path / (profile + ".json")),
                ]
            )
            reports.append(await v.evaluate(args))
    native, gemma = reports
    parent = gemma["materials"]["image"]
    derived = parent["model_input"]
    parent_bytes = Path(parent["path"]).read_bytes()
    derived_bytes = Path(derived["path"]).read_bytes()
    assert parent_bytes == v.make_png()
    assert derived_bytes != parent_bytes
    assert derived["parent_sha256"] == parent["sha256"] == v.hashlib.sha256(parent_bytes).hexdigest()
    assert derived["derived_sha256"] == v.hashlib.sha256(derived_bytes).hexdigest()
    mime, width, height = v.image_details(derived_bytes)
    assert derived["derived_size"] == {"width": width, "height": height}
    assert derived["parameters"]["interpolation"] == "Pillow.Resampling.BICUBIC"
    assert derived["parameters"]["crop"] is None and derived["official_sources"]
    payloads = [json.loads(request.content) for request in requests if request.method == "POST"]
    wire_image = next(part for part in payloads[1]["messages"][-1]["content"] if part["type"] == "image_url")
    assert wire_image["image_url"]["url"].startswith(f"data:{mime};base64,")
    assert v.base64.b64decode(wire_image["image_url"]["url"].split(",", 1)[1]) == derived_bytes
    assert native["materials"]["cases_sha256"] == gemma["materials"]["cases_sha256"]
    assert native["materials"]["character_public_sha256"] == gemma["materials"]["character_public_sha256"]
    for part in ("system", "user"):
        left = next(message["content"] for message in payloads[0]["messages"] if message["role"] == part)
        right = next(message["content"] for message in payloads[1]["messages"] if message["role"] == part)
        if part == "user":
            left = [item for item in left if item["type"] == "text"]
            right = [item for item in right if item["type"] == "text"]
        assert left == right


async def test_gemma_asset_profile_checks_original_manifest_and_keeps_actual_frame_identity(tmp_path):
    pytest.importorskip("PIL")
    raw = v.make_png(480, 270)
    source = tmp_path / "source.png"
    source.write_bytes(raw)
    entry = {
        "id": "source-frame",
        "cid": "837470340",
        "file": "source.png",
        "actual_time": 10.0,
        "requested_time": 11.25,
        "mime_type": "image/png",
        "width": 480,
        "height": 270,
        "bytes": len(raw),
        "image_sha256": v.hashlib.sha256(raw).hexdigest(),
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"bvid": "BV1We4y1k7za", "frames": [entry]}))
    images, records = v.load_asset_images(
        manifest_path=manifest,
        image_preprocessing="gemma4_image_280",
        artifact_directory=tmp_path / "derived",
    )
    model_input = records[0]["model_input"]
    assert records[0]["sha256"] == entry["image_sha256"] and source.read_bytes() == raw
    assert model_input["parent_size"] == {"width": 480, "height": 270}
    assert model_input["derived_size"] == {"width": 1056, "height": 576}
    assert model_input["parameters"]["expected_visual_tokens_from_grid"] == 264
    assert Path(model_input["path"]).read_bytes() == v.base64.b64decode(
        images[0]["data_url"].split(",", 1)[1]
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return sse({"content": final("图中有红圆和蓝方。", refs=["frame:1"])})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await v.run_case(
            {
                "id": "frame-task",
                "requires_assets": True,
                "turns": [{"text": "说说这张图。", "image": "assets"}],
            },
            v.Model("http://model", "candidate", client=client),
            v.ReadTools(None, None),
            character={},
            image_url="unused",
            asset_images=images,
        )
    text_part = next(
        part["text"] for part in requests[0]["messages"][-1]["content"] if part["type"] == "text"
    )
    evidence = json.loads(text_part.split("：", 1)[1].split("\n", 1)[0])["image_evidence"][0]
    assert evidence["ref"] == "frame:1" and evidence["cid"] == "837470340" and evidence["actual_time"] == 10
    assert (
        "requested_time" not in text_part
        and "parent_path" not in text_part
        and "derived_size" not in text_part
    )
    assert result["turns"][0]["protocol_delivery"]["complete"]
    entry["image_sha256"] = "0" * 64
    manifest.write_text(json.dumps({"bvid": "BV1We4y1k7za", "frames": [entry]}))
    with pytest.raises(v.EvaluationError, match="differ from manifest"):
        v.load_asset_images(
            manifest_path=manifest,
            image_preprocessing="gemma4_image_280",
            artifact_directory=tmp_path / "bad-derived",
        )
    assert not (tmp_path / "bad-derived").exists()
