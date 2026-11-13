import json

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.config import Settings
from tuber_studio.inference_profiles import DAFEIYU_KEY, DAFEIYU_MODEL
from tuber_studio.models import Character
from tuber_studio.operation_identity import identity_snapshot
from tuber_studio.providers import build_messages


@pytest.mark.parametrize("mode", ["live", "comment_draft", "direct_message"])
def test_runtime_reads_selected_card_with_legacy_metadata(store, mode):
    card = store.character() | {
        "core": {"identity": "新的角色设定"},
        "style": {"min_chars": 10, "max_chars": 80},
        "lore": [{"text": "珊瑚村的料理故事", "keywords": ["吃什么"]}],
        "persona_type": "fine_tuned",
        "fine_tuned_model": DAFEIYU_MODEL,
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


@pytest.mark.parametrize(
    ("route", "old_model", "model"),
    [("dafeiyu", "MiniCPM5-2B", DAFEIYU_MODEL), ("2b", DAFEIYU_KEY, "MiniCPM5-2B")],
)
def test_legacy_node_configuration_migrates_its_model(settings, route, old_model, model):
    base_url = "http://jetson.local:9012"
    data = settings.data
    data["inference_broker"].update(enabled=True, base_url=base_url)
    data["llm"].update(kind="llama_cpp", base_url=f"{base_url}/llm/{route}/", model=old_model)
    data["work_mode_profiles"] = {
        "text": {"llm": {**data["llm"], "kind": "openai_compatible"}},
        "live": {
            "llm": {**data["llm"], "kind": "openai_compatible", "base_url": "https://api.example/llm/2b"}
        },
    }
    settings.path.write_text(json.dumps(data), encoding="utf-8")

    restored = Settings(runtime=settings.runtime)
    llm = restored.data["llm"]
    profiles = restored.data["work_mode_profiles"]
    assert llm["base_url"] == base_url + "/llm" and llm["model"] == model
    assert profiles["text"]["llm"] == llm
    assert profiles["live"]["llm"] == data["work_mode_profiles"]["live"]["llm"]
    assert restored.endpoint_key("llm") == restored.inference_token
    assert restored.broker_key_for(f"{base_url}/llm/{route}") is None
    assert json.loads(settings.path.read_text())["llm"]["model"] == old_model

    restored.save_public(restored.data)
    assert json.loads(settings.path.read_text())["llm"]["base_url"] == base_url + "/llm"


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
        assert card["rules"] == card["lore"] == card["sources"] == []
        assert app.state.runtime.character == original
        path = f"/api/characters/{card['id']}"
        card = client.put(path, json=card | {"name": "新角色", "core": {"identity": "新设定"}},
                          headers=headers).raise_for_status().json()
        client.post(f"{path}/activate", headers=headers).raise_for_status()
        client.put(path, json=card | {"name": "改名后"}, headers=headers).raise_for_status()
        active = app.state.runtime.character
        assert active["id"] == card["id"] and active["name"] == "改名后"
        assert active["core"] == {"identity": "新设定"}
