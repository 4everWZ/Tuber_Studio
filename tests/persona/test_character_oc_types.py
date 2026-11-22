import json
from shutil import copyfile

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.core.identity import identity_snapshot
from tuber_studio.core.models import DEFAULT_CHARACTER_MANIFEST, DEFAULT_CHARACTER_VOICE, Character
from tuber_studio.operations.kernel.models import RESPONSE_INSTRUCTION, ResponseDecision
from tuber_studio.persona.context.message_builder import build_messages


@pytest.mark.parametrize("mode", ["live", "comment_draft", "direct_message"])
def test_runtime_reads_selected_card_with_legacy_metadata(store, mode):
    card = store.character() | {
        "core": {"identity": "新的角色设定"},
        "style": {"min_chars": 10, "max_chars": 80},
        "lore": [{"text": "珊瑚村的料理故事", "keywords": ["吃什么"]}],
        "persona_type": "fine_tuned",
        "fine_tuned_model": "archived-model",
        "compact_system_prompt": "旧模型绑定的短卡",
    }
    normalized = Character.model_validate(card).model_dump()
    assert not {"persona_type", "fine_tuned_model", "compact_system_prompt"} & normalized.keys()

    messages = build_messages(card, {"event_id": "e", "text": "今天吃什么？"}, {}, mode)
    assert "新的角色设定" in messages[0]["content"]
    assert "珊瑚村的料理故事" in messages[2]["content"]
    assert "旧模型绑定的短卡" not in json.dumps(messages, ensure_ascii=False)
    if mode == "live":
        assert "用10到80字回答。" in messages[-1]["content"]




def test_character_edit_overwrites_current_card_and_preserves_historical_sources(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        store, runtime = app.state.store, app.state.runtime
        assert [row["id"] for row in client.get("/api/characters", headers=headers).json()] == ["whale"]
        before = store.character()
        current = store.save_character(before)
        previous_identity = identity_snapshot({}, current)
        runtime.operations.configure({"enabled": True}, account_id="100", character=current)
        saved = client.put(
            "/api/characters/whale", json=current | {"core": {"identity": "更新后的角色"}}, headers=headers,
        ).raise_for_status().json()
        assert "version" not in saved
        assert store.character()["version"] == current["version"] and len(store.versions("whale")) == 2
        assert store.character("whale", before["version"]) == before
        assert identity_snapshot({}, store.character()) != previous_identity
        assert "_edit_identity" not in saved and runtime.operations.policy()["enabled"] is False
        assert client.get("/api/characters/whale/versions", headers=headers).status_code == 404
        repeated = identity_snapshot({}, store.character())
        client.put("/api/characters/whale", json=saved, headers=headers).raise_for_status()
        assert identity_snapshot({}, store.character()) == repeated
        assert "更新后的角色" in build_messages(store.character(), {"event_id": "e", "text": "你好"}, {})[0]["content"]

        other = client.post(
            "/api/characters/whale/clone", json={"id": "second-role", "name": "新角色"}, headers=headers,
        ).raise_for_status().json()
        client.post("/api/characters/second-role/activate", headers=headers).raise_for_status()
        assert runtime.character["id"] == other["id"] and other["name"] == "新角色"

        auto = client.post(
            "/api/characters/second-role/clone", json={"name": " 大肥鱼改良版 "}, headers=headers,
        ).raise_for_status().json()
        assert auto["id"].startswith("oc-") and auto["id"] != other["id"]
        assert auto["name"] == "大肥鱼改良版" and auto["core"] == other["core"]
        assert runtime.character["id"] == other["id"]


def test_create_blank_character_and_rename_before_and_after_activation(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        original = app.state.runtime.character
        card = client.post("/api/characters", json={"name": " 新卡片 "}, headers=headers).raise_for_status().json()
        assert card["id"].startswith("oc-") and card["name"] == "新卡片"
        assert card["core"] == card["style"] == {}
        assert card["voice_profile"] == DEFAULT_CHARACTER_VOICE
        assert card["avatar_manifest"] == DEFAULT_CHARACTER_MANIFEST
        assert card["rules"] == card["lore"] == card["sources"] == []
        assert app.state.runtime.character == original
        path = f"/api/characters/{card['id']}"
        card = client.put(path, json=card | {"name": "新角色", "aliases": ["小站长"], "core": {"identity": "新设定"}},
                          headers=headers).raise_for_status().json()
        client.post(f"{path}/activate", headers=headers).raise_for_status()
        client.put(path, json=card | {"name": "改名后"}, headers=headers).raise_for_status()
        active = app.state.runtime.character
        assert active["id"] == card["id"] and active["name"] == "改名后"
        assert active["aliases"] == ["小站长"]
        assert active["core"] == {"identity": "新设定"}


def test_web_display_name_does_not_edit_character_or_operator_grant(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + settings.admin_token}
        store, runtime = app.state.store, app.state.runtime
        before = store.character()
        runtime.operations.configure({"enabled": True}, account_id="100", character=before)
        path = "/api/characters/whale"
        card = client.get(path, headers=headers).raise_for_status().json()
        assert card["display_name"] == "大肥鱼"
        updated = client.put(path, json=card | {"display_name": " 戏台署名 "}, headers=headers).raise_for_status().json()
        assert updated["display_name"] == "戏台署名" and updated["name"] == "大肥鱼"
        assert store.character() == before and "display_name" not in runtime.character
        assert runtime.operations.policy()["enabled"] is True
        assert client.get("/api/chat/intelligent", headers=headers).json()["character"] == "戏台署名"
        assert client.get("/api/chat/private-call", headers=headers).json()["character"] == "戏台署名"
        cleared = client.put(path, json=updated | {"display_name": ""}, headers=headers).raise_for_status().json()
        assert cleared["display_name"] == ""
        assert client.get("/api/chat/intelligent", headers=headers).json()["character"] == "whale"


def test_blank_card_prefers_configured_yaoyao_recording(settings):
    path = settings.runtime / "tts/ref/yaoyao-reference32k.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    copyfile(settings.root / "assets/default/voice.wav", path)
    assert settings.default_character_voice() == "yaoyao_reference"
    settings.data["commercial_mode"] = True
    assert settings.default_character_voice() == DEFAULT_CHARACTER_VOICE


def test_character_list_uses_first_creation_time_across_versions_and_restore(store):
    original = store.character()
    older = store.save_character(original | {"id": "a-older", "name": "较早的角色"})
    newer = store.save_character(original | {"id": "z-newer", "name": "较新的角色"})
    for card, created_at in [(original, 1.0), (older, 2.0), (newer, 3.0)]:
        store.execute(
            "UPDATE character_versions SET created_at=? WHERE character_id=? AND version=?",
            (created_at, card["id"], card["version"]),
        )
    expected = [original["id"], older["id"], newer["id"]]
    assert [card["id"] for card in store.characters()] == expected

    store.save_character(older | {"name": "新版本"})
    store.activate(older["id"])
    store.replace_character(newer | {"name": "改名"})
    assert [card["id"] for card in store.characters()] == expected

    store.delete_character(newer["id"])
    assert [card["id"] for card in store.characters()] == expected[:-1]
    store.restore_character(newer["id"])
    assert [card["id"] for card in store.characters()] == expected


def test_paste_complete_card_delete_active_and_restore(settings):
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        store = app.state.store
        original = client.get("/api/characters/whale").raise_for_status().json()
        target = client.post("/api/characters", json={"name": "大肥鱼 v2"}).raise_for_status().json()
        path = f"/api/characters/{target['id']}"
        assert client.put(path, json=original | {"voice_profile": "missing"}).status_code == 422
        assert client.get(path).json() == target
        saved = client.put(path, json=original | {"name": "改名 v2"}).raise_for_status().json()
        assert saved == original | {"id": target["id"], "name": "改名 v2"}
        assert client.get("/api/characters/whale").json() == original
        client.post(f"{path}/activate").raise_for_status()
        history = store.character(target["id"])
        assert client.delete(path).status_code == 409
        assert client.request("DELETE", path, json={"replacement_id": "missing"}).status_code == 400
        assert app.state.runtime.character["id"] == target["id"]
        assert client.request("DELETE", path, json={"replacement_id": "whale"}).raise_for_status().json() == {
            "active_character_id": "whale",
        }
        assert [row["id"] for row in client.get("/api/characters").json()] == ["whale"]
        assert client.get("/api/characters/deleted").json() == [saved]
        assert client.get(path).status_code == 400
        assert client.put(path, json=saved).status_code == 400
        assert client.post(f"{path}/activate").status_code == 400
        assert store.character(target["id"], history["version"]) == history
        client.post(f"{path}/restore").raise_for_status()
        edited = client.put(path, json=saved | {"name": "恢复后改名"}).raise_for_status().json()
        assert client.get(path).json() == edited
        client.post(f"{path}/activate").raise_for_status()
        assert app.state.runtime.character["name"] == "恢复后改名"


def test_character_owner_uses_verified_profile_and_persists_per_card(settings, monkeypatch):
    app = create_app(settings)
    account = app.state.bilibili_account
    profile = {"uid": "12345678901234567", "name": "海边的号主",
               "avatar_url": "https://i0.hdslb.com/bfs/face/example.jpg"}
    lookup_uids = []

    async def lookup(uid):
        lookup_uids.append(uid)
        return profile

    monkeypatch.setattr(app.state.bilibili_account.adapter, "public_profile", lookup)
    path = "/api/characters/whale"
    preview = f"{path}/bilibili-owner"
    space = f"https://space.bilibili.com/{profile['uid']}/?spm_id_from=example"
    with TestClient(app, headers={"Authorization": "Bearer " + settings.admin_token}) as client:
        assert client.get(preview).status_code == 422
        assert client.get(preview, params={"account": "https://space.bilibili.com.example/12"}).status_code == 422
        card = client.get(path).raise_for_status().json()
        assert client.get(preview, params={"account": space}).raise_for_status().json() == profile
        saved = client.put(path, json=card | {
            "bilibili_owner": {"uid": profile["uid"], "name": "伪造的昵称"},
        }).raise_for_status().json()
        assert saved["bilibili_owner"] == app.state.runtime.character["bilibili_owner"] == profile
        assert lookup_uids == [profile["uid"]] * 2
        other = client.post("/api/characters", json={"name": "新角色"}).raise_for_status().json()
        assert "bilibili_owner" not in other
        client.put(path, json=card | {"name": "改名"}).raise_for_status()
        assert client.get(path).json()["bilibili_owner"] == profile
        assert lookup_uids == [profile["uid"]] * 2

        row = {"account_id": "99", "character_id": "whale", "character_version": app.state.runtime.character["version"]}
        _, messages, _ = app.state.browsing.decisions._model_input(
            row, ResponseDecision,
            {"thread": {"source": {"author_uid": profile["uid"], "text": "鱼鱼，你是谁家的？"}}},
            RESPONSE_INSTRUCTION,
        )
        header = messages[-1]["content"].removeprefix("公开来源材料：").split("\n\n所选材料：", 1)[0]
        reply_context = json.loads(header)
        assert reply_context["speaker_is_bilibili_owner"] is True
        assert reply_context["bilibili_owner"] == {"uid": profile["uid"], "name": profile["name"]}

        original_status = account.status
        monkeypatch.setattr(account, "status", lambda: original_status() | {"state": "logged_in", "profile": profile})
        assert client.get(preview).raise_for_status().json() == profile
        current = client.get(path).raise_for_status().json()
        cleared = client.put(path, json=current | {"bilibili_owner": None}).raise_for_status().json()
        assert "bilibili_owner" not in cleared and "bilibili_owner" not in app.state.runtime.character
        assert client.get(path).raise_for_status().json() == cleared


@pytest.mark.parametrize("mode", ["live", "comment_draft", "direct_message"])
def test_role_reply_recognizes_bilibili_owner_by_platform_uid(store, mode):
    profile = {"uid": "123", "name": "号主昵称", "avatar_url": "https://i0.hdslb.com/bfs/face/example.jpg"}
    card = store.character() | {"bilibili_owner": profile}
    event = {"event_id": "test", "text": "你是谁家的鱼呀？", "display_name": "号主昵称"}
    for platform, uid, expected in [("bilibili", "123", True), ("bilibili", "321", False),
                                    ("mock", "123", False), (None, None, False)]:
        messages = build_messages(card, event | {"platform": platform, "platform_user_id": uid}, {}, mode)
        context = json.loads(messages[2]["content"].removeprefix("上下文数据："))
        assert context["speaker_is_bilibili_owner"] is expected
        assert context["bilibili_owner"] == {"uid": "123", "name": "号主昵称"}
    assert "bilibili_owner" not in build_messages(store.character(), event, {}, mode)[2]["content"]


@pytest.mark.parametrize(("platform", "uid", "matches"), [
    ("bilibili", "123", True), ("bilibili", "321", False), ("mock", "123", None), (None, None, None),
])
def test_private_chat_participants_come_from_selected_card(store, platform, uid, matches):
    card = store.character()
    event = {"text": "再说说她吧", "platform": platform, "platform_user_id": uid}
    for owner in ({"uid": "123", "name": "第一位"}, {"uid": "123", "name": "改名后"}, None):
        messages = build_messages(card | {"bilibili_owner": owner}, event, {}, "operator_chat")
        scene = messages[1]
        assert scene["role"] == "system"
        participants = json.loads(scene["content"].split("私聊参与者：", 1)[1])
        assert participants["bilibili_owner"] == owner
        if owner and matches is not None:
            assert participants["speaker_is_bilibili_owner"] is matches
        else:
            assert "speaker_is_bilibili_owner" not in participants
        assert "bilibili_owner" not in messages[2]["content"]
