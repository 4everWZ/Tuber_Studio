"""Record ordinary Responses transport counts without saving model inputs, keys or reasoning."""

import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
from tuber_studio import responses_provider


@contextmanager
def admission_wire(provider, record):
    """Inspect the actual outgoing QA contract, excluding headers and chat contents."""
    receipts = []
    async def observed(request):
        if not request.url.path.endswith("/chat/completions"):
            return
        body = json.loads(request.content)
        fmt = body.get("response_format", {})
        schema = fmt.get("json_schema", {}).get("schema") or fmt.get("schema", {})
        if tag := body.get("structured_outputs", {}).get("structural_tag"):
            schema = json.loads(tag)["format"]["json_schema"]
        if schema.get("title") not in {"MemoryBatch", "RoutineReviews", "OwnerUtterance", "AgreementReview", "ChangeOfPractice"}:
            return
        messages = body.get("messages", [])
        receipts.append({"provider": type(provider).__name__, "method": getattr(provider, "generate_operator_json", provider.generate_json).__name__,
                         "path": request.url.path, "model": body["model"], "format": fmt.get("type", "structural_tag"),
                         "schema": schema, "max_tokens": body["max_tokens"],
                         "identity_block": any("<current-account-identity>" in m["content"] for m in messages),
                         "public_example": any("冷泡茶" in m["content"] for m in messages)})
        record("admission_wire", receipts)
    hooks = provider.client.event_hooks["request"]
    hooks.append(observed)
    try:
        yield
    finally:
        hooks.remove(observed)


async def load_capacity(config, model):
    root, key = config
    async with httpx.AsyncClient(base_url=root + "/llm/v1/", headers={"Authorization":"Bearer " + key},
                                 trust_env=False, timeout=10) as client:
        try:
            response = await client.get("models")
            response.raise_for_status()
            entry = next((m for m in response.json().get("data", []) if m.get("id") == model), {})
            return {k:entry[k] for k in ("id", "max_model_len", "context_window") if k in entry}
        except (httpx.HTTPError, ValueError):
            return {}


@contextmanager
def wire_receipts(record):
    send = responses_provider.response_json
    receipts = []
    async def observed(client, method, path, *, payload):
        def size(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
        entry = {"requested_output_tokens":payload["max_output_tokens"], "model":payload["model"],
                 "input_bytes":size(payload["input"]), "tools_bytes":size(payload["tools"]),
                 "tool_names":[t["name"] for t in payload["tools"]]}
        async def status(response):
            entry["http_status"] = response.status_code
            if response.is_error:
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) <= 16384:
                    await response.aread()
                    try:
                        error = response.json().get("error") or {}
                        entry["error_code"] = error.get("code") if isinstance(error, dict) else None
                    except ValueError:
                        pass
                if retry := response.headers.get("Retry-After"):
                    entry["retry_after"] = retry
        hooks = client.event_hooks["response"]
        hooks.append(status)
        try:
            result = await send(client, method, path, payload=payload)
            entry.update(status=result.get("status"), incomplete_details=result.get("incomplete_details"),
                         usage=result.get("usage"))
            return result
        finally:
            hooks.remove(status)
            receipts.append(entry)
            record("responses_receipts", receipts)
    with patch.object(responses_provider, "response_json", observed):
        yield
