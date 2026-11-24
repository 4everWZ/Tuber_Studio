import json

import pytest

from tests.support.stores import FIXTURES, create_store
from tuber_studio.core.config import ROOT
from tuber_studio.core.models import Character, canonical_voice_id
from tuber_studio.core.store import dump
from tuber_studio.operations.kernel.control import OperationsControl
from tuber_studio.persona.characters.archive import archive_character_version
from tuber_studio.persona.characters.presets import _fingerprint
from tuber_studio.persona.context.message_builder import build_messages


def presets():
    return json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))


def legacy():
    return json.loads(
        (FIXTURES / "characters-before-20261101.json").read_text(encoding="utf-8")
    )


def clean_legacy():
    """The complete application-owned clean preset shipped before enrichment."""
    items = legacy()
    items[0]["core"]["identity"] = (
        "你是大肥鱼，又叫鲸鱼娘：蓝白配色、蓝色长发和鲸鱼尾巴的 Q 版主播。"
        "你喜欢白饭和摸鱼，把观众当作来海底串门的朋友，一边逗趣一边陪大家聊天。"
    )
    items[0]["rules"] = [
        "说话带点嘴硬和俏皮，关心常藏在小小的吐槽后面。",
        "跟着对方的情绪交流；听到难过的事，先陪伴、认真倾听。",
        "记得熟悉观众的称呼和共同话题，让亲近感随着相处慢慢变化。",
        "遇到感兴趣的话题会主动追问，认真聊事时表达清楚。",
    ]
    items[1]["core"]["identity"] = (
        "你是大肥鱼，又叫鲸鱼娘：有蓝色长发和鲸鱼尾巴的 Q 版主播。"
        "深夜，你守着海底电台，用温柔平静的声音陪夜归的朋友聊天。"
    )
    items[1]["rules"] = [
        "给对方留出慢慢说话的空间，认真回应话里的情绪。",
        "熟悉的人来了，顺着彼此聊过的话题自然接话。",
        "想起海底夜色、潮声和热饭时，会温柔地分享自己的小念头。",
    ]
    return items


def test_startup_does_not_reintroduce_an_archived_preset(tmp_path):
    path = tmp_path / "archived.sqlite3"
    store = create_store(path, legacy())
    archive_character_version(store, "whale_calm", 1, "旧版角色退役")
    store.close()
    store = create_store(path, presets())
    assert "whale_calm" not in {item["id"] for item in store.characters()}
    assert store.character("whale_calm", 1)["version"] == 1
    assert store.versions("whale_calm") == []
    store.close()


@pytest.mark.parametrize("custom", [False, True])
def test_resident_fields_preserve_previous_active_version_and_history(tmp_path, custom):
    previous = presets()[0]
    del previous["self_description"], previous["speaking_style"]
    previous["voice_profile"] = "yaoyao_sovits_v2"
    if custom:
        previous["core"]["identity"] = "你是蓝发鲸鱼娘，住在海底小屋，机灵傲娇又爱摸鱼。"
        previous["style"]["instructions"] = "说话口语化，平常用「我」和「你」。"
    path = tmp_path / "profiles.sqlite3"
    old = create_store(path, [previous])
    original = old.character()
    history = old.all("SELECT * FROM character_versions")
    old.close()
    reopened = create_store(path, presets())
    try:
        assert reopened.character() == original
        assert reopened.all("SELECT * FROM character_versions") == history
    finally:
        reopened.close()


def test_enriched_preset_keeps_character_assets_without_retired_radio():
    before, after = clean_legacy(), presets()
    assert _fingerprint(before[0]) == "c502c1ff0d81547625a819dd59aec9f2073a3610a51c86a4b75ddebd070df598"
    for item in after:
        Character.model_validate(item)
    for field in ("id", "avatar_manifest", "license_status"):
        assert after[0][field] == before[0][field]
    assert [item["id"] for item in after] == ["whale"]
    main = after[0]
    assert main["name"] == "Aqua"
    assert main["display_name"] == "Aqua"
    assert main["voice_profile"] == "whale_reference"
    assert main["style"]["min_chars"] == 1
    assert main["style"]["max_chars"] == 120
    assert {"Aqua", "本鲸"} <= set(main["style"]["catchphrases"])
    # The persona update removes cliches and stacked fanfic props from the permanent shell.
    dumped_shell = json.dumps(
        [main["core"], main["style"], main["rules"], main["lore"]], ensure_ascii=False
    )
    for retired in ("深海思考", "贝壳灯", "软垫", "白饭", "女仆裙", "算力挂件"):
        assert retired not in dumped_shell
    assert len(main["core"]["examples"]) == 4
    assert main["lore"] == []
    assert main["core"]["background"] and main["core"]["interaction"]
    for item in main["core"]["examples"]:
        assert item["viewer"]
        assert 1 <= len(item["reply"]) <= main["style"]["max_chars"]


def test_character_sources_stay_out_of_prompt_and_lore_is_bounded():
    role = presets()[0]
    event = {"event_id": "preset-probe", "text": "晚上好"}
    messages = build_messages(role, event, {})
    context = json.loads(messages[2]["content"].removeprefix("上下文数据："))
    assert context["active_lore"] == []
    assert all(source not in "\n".join(m["content"] for m in messages) for source in role["sources"])
    assert role["license_status"] not in "\n".join(m["content"] for m in messages)
    fixed = json.loads(messages[0]["content"].split("角色设定：", 1)[1])
    assert fixed["core"] == role["core"]
    assert "\n".join(msg["content"] for msg in messages).count(role["core"]["examples"][0]["reply"]) == 1
    role_with_lore = role | {
        "lore": [
            {"keywords": ["珊瑚"], "text": "珊瑚故事一"},
            {"keywords": ["潮汐"], "text": "潮汐故事二"},
            {"keywords": ["气泡"], "text": "气泡故事三"},
            {"keywords": ["星光"], "text": "星光故事四"},
        ]
    }
    event["text"] = " ".join(item["keywords"][0] for item in role_with_lore["lore"])
    messages = build_messages(role_with_lore, event, {})
    context = json.loads(messages[2]["content"].removeprefix("上下文数据："))
    assert context["active_lore"] == [item["text"] for item in role_with_lore["lore"][:3]]


@pytest.mark.parametrize(
    ("previous_kind", "old_voice"),
    [
        ("engineering", "yaoyao_sovits_v2"),
        ("engineering", "whale_sovits_v2"),
        ("clean", "yaoyao_sovits_v2"),
        ("clean", "whale_sovits_v2"),
    ],
)
def test_upgrade_appends_preserves_history_voice_relations_and_drafts(tmp_path, previous_kind, old_voice):
    previous = clean_legacy() if previous_kind == "clean" else legacy()
    previous[0]["voice_profile"] = old_voice
    path = tmp_path / "roles.sqlite3"
    store = create_store(path, previous)
    originals = {c["id"]: store.character(c["id"]) for c in previous}
    store.set("active_character", "whale_calm")
    store.execute("INSERT INTO relations VALUES('whale',7,?)", (dump({"affection": 18}),))
    store.execute(
        "INSERT INTO drafts(id,comment_id,text,status,character_id,character_version) "
        "VALUES(1,1,'原草稿','approved','whale',1)"
    )
    draft = store.one("SELECT * FROM drafts WHERE id=1")
    history_rows = store.all("SELECT * FROM character_versions ORDER BY character_id,version")
    store.close()

    store = create_store(path, presets())
    assert store.get("active_character") == "whale_calm"
    for target in presets():
        before = originals[target["id"]]
        current = store.character(target["id"])
        target = Character.model_validate(target).model_dump()
        version = 1 if before == target else 2
        assert current["version"] == version
        assert store.character(target["id"], 1) == before
        assert current == before | {
            "version": version,
            "core": target["core"],
            "style": target["style"],
            "rules": target["rules"],
            "lore": target["lore"],
            "sources": target["sources"],
            "license_status": target["license_status"],
        }
        assert (current["name"], current["voice_profile"], current["avatar_manifest"]) == (
            before["name"],
            before["voice_profile"],
            before["avatar_manifest"],
        )
    assert store.relationship("whale", 7)["affection"] == 18
    assert store.one("SELECT * FROM drafts WHERE id=1") == draft
    assert (
        store.all("SELECT * FROM character_versions WHERE version=1 ORDER BY character_id,version")
        == history_rows
    )
    # An explicit manual publish may use retained drafts from this same OC.
    # Automatic publication still requires current version authorization.
    control = OperationsControl(store)
    store.set("active_character", "whale")
    control.assert_allowed(automatic=False, channel="comments", account_id="42", character=originals["whale"])
    control.configure({"enabled": True}, account_id="42", character=store.character("whale"))
    with pytest.raises(ValueError, match="角色版本已变化"):
        control.assert_allowed(
            automatic=True, channel="comments", account_id="42", character=originals["whale"]
        )
    store.close()

    store = create_store(path, presets())
    assert len(store.versions("whale")) == 2
    store.activate("whale", 1)
    store.close()
    store = create_store(path, presets())
    assert store.character("whale")["version"] == 1
    assert len(store.versions("whale")) == 2
    store.close()


@pytest.mark.parametrize("previous_kind", ["engineering", "clean"])
@pytest.mark.parametrize(
    "field", ["identity", "rules", "traits", "style", "voice", "metadata", "name", "avatar", "lore"]
)
def test_custom_active_content_is_untouched(tmp_path, field, previous_kind):
    items = clean_legacy() if previous_kind == "clean" else legacy()
    role = items[0]
    if field == "identity":
        role["core"]["identity"] += "，喜欢贝壳。"
    elif field == "rules":
        role["rules"].append("喜欢打招呼。")
    elif field == "traits":
        role["core"]["traits"].append("害羞")
    elif field == "style":
        role["style"]["max_chars"] = 61
    elif field == "voice":
        role["voice_profile"] = "my_voice"
    elif field == "name":
        role["name"] = "自家的鱼鱼"
    elif field == "avatar":
        role["avatar_manifest"] = "assets/my-character/manifest.json"
    elif field == "lore":
        role["lore"].append({"keywords": ["贝壳"], "text": "用户自己的贝壳故事。"})
    else:
        role["sources"].append("https://example.com/my-character")
    path = tmp_path / "custom.sqlite3"
    store = create_store(path, items)
    before = store.character("whale")
    store.close()
    store = create_store(path, presets())
    assert store.character("whale") == before
    assert len(store.versions("whale")) == 1
    store.close()


@pytest.mark.parametrize("historical_unknown", [False, True])
@pytest.mark.parametrize("previous_kind", ["engineering", "clean"])
def test_custom_or_unknown_historical_content_protects_rolled_back_default(
    tmp_path, historical_unknown, previous_kind
):
    path = tmp_path / "history.sqlite3"
    store = create_store(path, clean_legacy() if previous_kind == "clean" else legacy())
    custom = store.character("whale")
    if historical_unknown:
        custom["unknown_extension"] = {"owner": "user"}
        custom["version"] = 2
        store.execute("INSERT INTO character_versions VALUES('whale',2,?,0)", (dump(custom),))
    else:
        custom["name"] = "我的大肥鱼"
        store.save_character(custom)
    store.activate("whale", 1)
    before = store.versions("whale")
    store.close()
    store = create_store(path, presets())
    assert store.character("whale")["version"] == 1
    assert store.versions("whale") == before
    store.close()


def test_operator_edited_seeds_are_not_migration_targets(tmp_path):
    path = tmp_path / "operator.sqlite3"
    store = create_store(path, legacy())
    before = store.character("whale")
    store.close()
    targets = presets()
    targets[0]["rules"].append("操作者自定义。")
    store = create_store(path, targets)
    assert store.character("whale") == before
    assert len(store.versions("whale")) == 1
    store.close()


@pytest.mark.parametrize("with_resident_fields", [False, True])
def test_known_old_voice_history_upgrades_without_replacing_selected_voice(tmp_path, with_resident_fields):
    path = tmp_path / "known-history.sqlite3"
    items = legacy()
    items[0]["voice_profile"] = "whale_sovits_v2"
    store = create_store(path, items)
    second = legacy()[0]
    store.save_character(second)
    original_rows = store.all("SELECT * FROM character_versions WHERE character_id='whale' ORDER BY version")
    store.close()
    targets = presets()
    if not with_resident_fields:
        del targets[0]["self_description"], targets[0]["speaking_style"]
        targets[0]["voice_profile"] = "yaoyao_sovits_v2"
    store = create_store(path, targets)
    assert store.character("whale")["version"] == 3
    assert store.character("whale")["voice_profile"] == "yaoyao_reference"
    assert (
        store.all(
            "SELECT * FROM character_versions WHERE character_id='whale' AND version<3 ORDER BY version"
        )
        == original_rows
    )
    store.close()


@pytest.mark.parametrize("old_voice", ["yaoyao_sovits_v2", "whale_sovits_v2"])
def test_known_engineering_and_clean_history_upgrade_once_and_allow_rollback(tmp_path, old_voice):
    path = tmp_path / "clean-history.sqlite3"
    original = legacy()
    original[0]["voice_profile"] = old_voice
    clean = clean_legacy()[0]
    clean["voice_profile"] = old_voice
    store = create_store(path, original)
    store.set("active_character", "whale_calm")
    store.execute("INSERT INTO relations VALUES('whale',7,?)", (dump({"affection": 27}),))
    store.save_character(clean)
    old_versions = store.versions("whale")
    store.close()
    store = create_store(path, presets())
    assert store.character("whale")["version"] == 3
    assert store.versions("whale")[1:] == old_versions
    assert store.character("whale")["core"] == presets()[0]["core"]
    assert store.character("whale")["voice_profile"] == canonical_voice_id(old_voice)
    assert store.relationship("whale", 7)["affection"] == 27
    assert store.get("active_character") == "whale_calm"
    store.activate("whale", 2)
    store.close()
    store = create_store(path, presets())
    assert store.character("whale")["version"] == 2
    assert len(store.versions("whale")) == 3
    store.close()


def test_sample_voice_history_allows_the_current_production_preset_upgrade(tmp_path):
    path = tmp_path / "sample-history.sqlite3"
    original = legacy()
    original[0]["voice_profile"] = "whale_sample_v1"
    store = create_store(path, original)
    selected = store.character("whale") | {"voice_profile": "whale_sovits_v2"}
    store.save_character(selected)
    history = store.versions("whale")
    store.close()
    store = create_store(path, presets())
    assert store.character("whale")["version"] == 3
    assert store.character("whale")["core"] == presets()[0]["core"]
    assert store.character("whale")["voice_profile"] == "whale_reference"
    assert store.versions("whale")[1:] == history
    store.close()


def test_unknown_active_fields_are_preserved_without_upgrade(tmp_path):
    path = tmp_path / "unknown-active.sqlite3"
    store = create_store(path, legacy())
    custom = store.character("whale") | {"user_extension": {"affection_style": "gentle"}}
    store.execute("UPDATE character_versions SET config=? WHERE character_id='whale'", (dump(custom),))
    store.close()
    store = create_store(path, presets())
    assert store.character("whale") == custom
    assert len(store.versions("whale")) == 1
    store.close()


def test_fresh_install_uses_clean_presets_without_extra_version(tmp_path):
    store = create_store(tmp_path / "fresh.sqlite3", presets())
    assert all(role["version"] == 1 for role in store.characters())
    assert all(len(store.versions(role["id"])) == 1 for role in presets())
    for field in ("self_description", "speaking_style"):
        assert store.character("whale")[field] == presets()[0][field]
    store.close()


def test_whale_preset_rejects_dafeiyu_self_reference_and_upgrades_from_prior_seed(tmp_path):
    role = presets()[0]
    assert role["name"] == "Aqua" and role["display_name"] == "Aqua"
    assert "大肥鱼" not in role["style"]["catchphrases"]
    assert "Aqua" in role["self_description"]
    assert "大肥鱼" not in role["self_description"]
    assert any(
        "Aqua" in example["reply"] or "本鲸" in example["reply"] or "我" in example["reply"]
        for example in role["core"]["examples"]
    )
    for example in role["core"]["examples"]:
        assert "大肥鱼陪" not in example["reply"] and "大肥鱼就是" not in example["reply"]

    # Simulate an existing DB seeded with clean_legacy and verify upgrade to the polished whale card
    prior_path = tmp_path / "prior-seed.sqlite3"
    prior = clean_legacy()
    prior[0]["voice_profile"] = "whale_reference"
    prior_store = create_store(prior_path, prior)
    prior_store.close()

    upgraded = create_store(prior_path, presets())
    current = upgraded.character("whale")
    assert current["version"] == 2
    assert current["core"] == role["core"]
    assert current["rules"] == role["rules"]
    upgraded.close()
