"""Observe editor calls on the isolated replay client's outbound chat transport."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from unittest.mock import patch

from tuber_studio.operation_identity import captured_identity


@contextmanager
def editor_requests(provider, goal, character_name, publish):
    pending = ContextVar("editor_request", default=None)
    generate = provider.generate_json

    async def observed(messages, schema, **kwargs):
        fields = set(schema.get("properties", {}))
        route = schema.get("title")
        if not route:
            if fields == {"anchor", "detail", "use"}:
                route = "pixel_caption"
            elif any(field.startswith(("items.", "gaps.")) for field in fields):
                route = "field_repair"
            else:
                route = "other"
        token = pending.set(route)
        try:
            return await generate(messages, schema, **kwargs)
        finally:
            pending.reset(token)

    async def sent(request):
        if not pending.get() or not request.url.path.endswith("/chat/completions"):
            return
        messages = json.loads(request.content)["messages"]
        system = [m["content"] for m in messages if m["role"] == "system" and isinstance(m["content"], str)]
        # Look only at text parts; image bytes, headers, account data and prompts stay out of the receipt.
        user_text = []
        image_count = 0
        for message in messages:
            if message["role"] != "user":
                continue
            content = message["content"]
            if isinstance(content, str):
                user_text.append(content)
            elif isinstance(content, list):
                user_text.extend(part["text"] for part in content if part.get("type") == "text")
                image_count += sum(part.get("type") == "image_url" for part in content)
        publish({"schema": pending.get(), "account_identity_block": captured_identity(messages) is not None,
                 "character_name_in_system": any(character_name and character_name in s for s in system),
                 "task_goal_in_user_text": any(goal and goal in s for s in user_text),
                 "image_count": image_count})

    hooks = provider.client.event_hooks["request"]
    with patch.object(provider, "generate_json", observed):
        hooks.append(sent)
        try:
            yield
        finally:
            hooks.remove(sent)
