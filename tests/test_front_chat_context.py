import json
from uuid import uuid4

import httpx
import pytest

from tuber_studio.app import create_app
from tuber_studio.front_persona import front_identity
from tuber_studio.models import Reply, resident_character_profile
from tuber_studio.operation_identity import provider_identity_messages


@pytest.mark.parametrize("fields", [
    {},
    {"core": {"identity": "你是领航员，住在港口。"}, "style": {"instructions": "语气平静。"}},
    {"self_description": "自述，" * 40, "speaking_style": "口吻。" * 40},
    {"speaking_style": "随和，喜欢用短句。"},
])
def test_front_identity_renders_the_complete_resident_profile(fields):
    card = {"name": "小舟", **fields}
    profile = resident_character_profile(card)
    rendered = dict(line.split("：", 1) for line in front_identity(card).splitlines())
    labels = {"name": "角色名", "self_description": "身份", "speaking_style": "风格"}
    assert rendered == {labels[key]: text for key, text in profile.items()}


@pytest.mark.parametrize("temporary", [False, True])
@pytest.mark.parametrize("semantic", [False, True])
async def test_chat_model_packet_keeps_account_out_and_uses_current_question(
    settings, monkeypatch, temporary, semantic,
):
    app = create_app(settings)
    runtime = app.state.runtime
    provider = runtime.llm
    provider.identity_resolver = lambda: {"account_bio": "运营账号探针", "identity_version": "test"}
    calls = []

    def capture(messages):
        wire = provider_identity_messages(provider, messages)
        calls.append(wire)
        assert "运营账号探针" not in json.dumps(wire, ensure_ascii=False)
        assert front_identity(runtime.character) in wire[0]["content"]
        assert runtime.character["core"]["identity"] not in wire[0]["content"]

    question = "帮我数一下有几个字。"
    if semantic:
        async def generate(messages, schema, **kwargs):
            capture(messages)
            if len(calls) == 1:
                raise ValueError("测试本轮重试")
            return {"action": "chat", "reply_markdown": "好。"}
        monkeypatch.setattr(provider, "generate_assistant_json", generate, raising=False)
    else:
        async def generate(messages, character, event_id, source_text):
            capture(messages)
            return Reply(spoken_text="好。", reply_to=[event_id])
        monkeypatch.setattr(provider, "generate", generate)

    body = {"message": question, "request_id": str(uuid4()), "temporary": temporary}
    if temporary:
        body["window"] = [{"role": "user", "content": "上一句在这里"}, {"role": "assistant", "content": "我在听"}]
    elif not semantic:
        body["intent"] = {"action": "chat"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1") as client:
        if semantic:
            assert (await client.post("/api/chat/intelligent", json=body)).status_code == 503
        reply = (await client.post("/api/chat/intelligent", json=body)).raise_for_status().json()
    assert reply["reply_text"] == "好。"
    if semantic:
        assert calls[0][0] == calls[1][0]
        assert calls[0][-1] == calls[1][-1]
    dialogue = [m["content"] for m in calls[-1] if m["role"] != "system"]
    assert question in dialogue[-1]
    if temporary:
        assert dialogue[-3:-1] == ["上一句在这里", "我在听"]
    assert "运营账号探针" in json.dumps(provider_identity_messages(provider, []), ensure_ascii=False)
    await app.state.comments.adapter.close()
    app.state.store.close()
