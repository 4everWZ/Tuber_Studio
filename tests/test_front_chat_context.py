import json
from uuid import uuid4

import httpx
import pytest

from tuber_studio.app import create_app
from tuber_studio.agent.operator_chat import OperatorChat
from tuber_studio.front_persona import front_identity, persona_instructions
from tuber_studio.models import Reply, resident_character_profile
from tuber_studio.operation_identity import provider_identity_messages
from tuber_studio.providers import single_system_messages


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
    runtime.store.replace_character(runtime.character | {"bilibili_owner": {"uid": "101", "name": "绑定号主"}})
    provider = runtime.llm
    provider.identity_resolver = lambda: {"account_bio": "运营账号探针", "identity_version": "test"}
    monkeypatch.setattr(runtime.account, "status", lambda: {
        "state": "logged_in", "verification_stale": False,
        "profile": {"uid": "202", "name": "运营频道", "bio": "运营账号探针"},
    })
    calls = []

    def capture(messages):
        wire = single_system_messages(provider_identity_messages(provider, messages))
        calls.append(wire)
        assert "运营账号探针" not in json.dumps(wire, ensure_ascii=False)
        assert wire[0]["content"].count(persona_instructions(runtime.character)) == 1
        assert runtime.character["core"]["identity"] in wire[0]["content"]
        identities = next(row["content"].split("私聊参与者：", 1)[1] for row in wire if "私聊参与者：" in row["content"])
        facts = json.loads(identities.splitlines()[0])
        assert facts["bilibili_owner"] == {"uid": "101", "name": "绑定号主"}
        assert (facts["operating_account"]["uid"], facts["operating_account"]["name"]) == ("202", "运营频道")

    question = "帮我数一下有几个字。"
    if semantic:
        async def generate(messages, schema, **kwargs):
            if schema.get("title") == "TurnRoute":
                return {"route": "chat", "action": "chat"}
            assert "reply_markdown" in schema["properties"]
            capture(messages)
            if len(calls) == 1:
                raise ValueError("测试本轮重试")
            return {"reply_markdown": "好。"}
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


async def test_window_slides_whole_turns_without_changing_the_resident_context():
    class Provider:
        capacity_window = 3000

        async def count_tokens(self, messages):
            return sum(len(row["content"]) for row in messages)

    resident = [{"role": "system", "content": "OC" * 350}, {"role": "system", "content": "记忆" * 150}]
    old = [{"role": "user", "content": "旧问题" * 350}, {"role": "assistant", "content": "旧回复" * 350}]
    recent = [{"role": "user", "content": "最近问题" * 80}, {"role": "assistant", "content": "最近回复" * 80}]
    current = {"role": "user", "content": "当前问题"}
    packed = await OperatorChat._fit_assistant_history(Provider(), resident, old + recent, current, None, 1800)
    assert packed == resident + recent + [current]
