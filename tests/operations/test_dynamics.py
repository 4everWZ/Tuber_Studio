import asyncio
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tuber_studio.infra.platforms.bilibili.client import PublishUncertain
from tuber_studio.infra.web_client import fetch as web_fetch
from tuber_studio.operations.channels.dynamics import MAX_SAFE_INTEGER, Dynamics
from tuber_studio.operations.channels.dynamics import attachments as attachments_module
from tuber_studio.operations.channels.dynamics import evaluator as evaluator_module
from tuber_studio.operations.channels.dynamics import publisher as module
from tuber_studio.persona.memory.core_memory import GroundedMemory


class Model:
    def __init__(self):
        self.calls = []
        self.schemas = []
        self.count_calls = []
        self.output = None
        self.summary_output = None
        self.summary_hook = None
        self.hook = None
        self.illustration = None

    async def generate_json(self, messages, schema, *, max_tokens):
        self.calls.append(messages)
        self.schemas.append(schema)
        payload = json.loads(messages[1]["content"])
        if payload.get("task") == "operations_memory_summary":
            if self.summary_hook:
                await self.summary_hook()
            return (
                json.loads(self.summary_output)
                if self.summary_output is not None
                else (
                    {
                        "entries": [
                            {"source_id": source["id"], "quote": source["text"]}
                            for source in payload["public_sources"]
                        ]
                    }
                )
            )
        if self.hook:
            await self.hook()
        if self.output is None and not payload["public_sources"]:
            return {"action": "wait", "reason": "这次先不发", "content": "", "source_ids": [],
                    "next_evaluation_seconds": 1800}
        source = next(iter(payload["public_sources"]), {})
        discussion = source.get("type") in {"public_incident", "published_reply"}
        return (
            json.loads(self.output)
            if self.output is not None
            else (
                {
                    "action": "post",
                    "reason": "和大家分享公开的虚构故事",
                    "content": "海底小剧场：今天又多盛了一碗饭。",
                    "source_ids": [source["id"]],
                    "link_source_ids": [source["id"]] if discussion else [],
                    "illustration": self.illustration or ("comment_card" if discussion else "none"),
                    "next_evaluation_seconds": 1800,
                }
            )
        )

    async def count_tokens(self, messages):
        self.count_calls.append(messages)
        return len(messages[0]["content"]) // 2 + 1


@pytest.fixture
def service(settings, store, monkeypatch):
    account = SimpleNamespace(
        adapter=object(),
        credential_lock=asyncio.Lock(),
        credentials={},
        credentials_configured=True,
        display_account_id=lambda: "",
    )
    comments = SimpleNamespace(account=account)
    runtime = SimpleNamespace(operation_llm=Model(), generation_lock=asyncio.Lock())
    runtime.offline_generation = lambda lane: runtime.generation_lock
    service = Dynamics(settings, store, runtime, comments, account=account)
    service.operations.configure(
        {"enabled": True}, account_id="injected-test-account", character=store.character()
    )
    service.sent = []

    async def send(adapter, text, *, before_send=None):
        if before_send:
            before_send()
        service.sent.append((adapter, text))
        return {"platform": "bilibili", "code": 0, "dynamic_id": "12345"}

    monkeypatch.setattr(module, "publish_text", send)
    return service


def memory(service, text="在虚构的海底厨房，我又盛了一碗饭。", *, role="assistant", kind=None):
    GroundedMemory(service.store).record(
        "test", service.store.character(), str(uuid4()), role, text, kind=kind
    )
    return service.store.one("SELECT MAX(id) AS id FROM grounded_memory")["id"]


def public_memory(service, **kwargs):
    identifier = memory(service, **kwargs)
    service.mark_memory(identifier, True)
    return identifier


def published_reply(service):
    char = service.store.character()
    cid = service.store.execute(
        "INSERT INTO comments(video_id,comment_id,content) VALUES(?,?,?)",
        ("BV1d8pU6xEZX", "42", "鱼鱼的海底厨房是什么样？"),
    ).lastrowid
    return service.store.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,receipt) VALUES(?,?,?,?,?,?)",
        (cid, "想象中的海底厨房有热饭。", "published", char["id"], char["version"], '{"rpid":"77"}'),
    ).lastrowid


def due(service):
    service.store.set(
        "dynamic_schedule", {"next_evaluation_at": 0, "backoff_until": None, "last_error": None}
    )


async def test_public_incident_is_factual_cited_and_removed_from_draft_sources(service):
    from tuber_studio.persona.memory.daily import DailyMemoryService

    memory = DailyMemoryService(service.store)
    char = service._character()
    identifier = memory.record_public_incident(
        char["id"],
        char["version"],
        incident_key="real-comment-mixup",
        highlight_date="2026-11-07",
        headline="评论接错话",
        anecdote="我没认清对话对象，观众问我是不是回错了。",
        public_sources=[
            {
                "bvid": "BV13S4i6fE82",
                "thread_id": "323202459488",
                "comment_id": "323231188736",
                "quote": "你消息是不是回错了",
            }
        ],
    )
    decision = await service.evaluate(manual=True)
    post = service._post(decision["post_id"])
    assert post["status"] == "pending_review"
    assert not service.sent
    assert post["sources"][0]["id"] == f"incident:{identifier}"
    assert post["sources"][0]["fictional"] is False
    payload = json.loads(service.runtime.operation_llm.calls[-1][1]["content"])
    assert payload["public_sources"][0]["fictional"] is False
    assert json.dumps(payload["public_sources"], ensure_ascii=False).count("你消息是不是回错了") == 1
    original, = post["comment_card"]["comments"]
    assert original["quote"] == "你消息是不是回错了" and original["author"] == "作者资料未保存"
    assert post["comment_card"]["links"] == [{
        "bvid": "BV13S4i6fE82", "url": "https://www.bilibili.com/video/BV13S4i6fE82#reply323231188736",
    }]
    assert service._valid_sources(post)
    memory.delete_highlight(identifier)
    assert not service._valid_sources(post)


async def test_dynamic_from_public_reply_publishes_the_reviewed_text_and_comment_card(service, monkeypatch):
    from io import BytesIO

    from PIL import Image, ImageFont

    from tuber_studio.operations.channels.dynamics import comment_card as dynamic_comment_card

    monkeypatch.setattr(dynamic_comment_card, "_font", lambda size: ImageFont.load_default())
    draft_id = published_reply(service)
    service.store.execute(
        "UPDATE comments SET display_name='观众甲',platform_user_id='11',parent_comment_id='41',created_at='10' "
        "WHERE comment_id='42'"
    )
    service.store.execute(
        "INSERT INTO comments(video_id,comment_id,content,display_name,platform_user_id,created_at) "
        "VALUES('BV1d8pU6xEZX','41','你说的是哪间厨房？','观众乙','12','5')"
    )
    service.store.execute(
        "UPDATE drafts SET answer_check=? WHERE id=?",
        (json.dumps({"generation_identity": {"account_uid": "100", "account_display_name": "小鲸鱼"}}), draft_id),
    )
    decision = await service.evaluate(manual=True)
    post_id = decision["post_id"]
    post_card = service._post(post_id)["comment_card"]
    assert [(item["comment_id"], item["author"]) for item in post_card["comments"]] == [
        ("41", "观众乙"), ("42", "观众甲"), ("77", "小鲸鱼"),
    ]
    assert len(post_card["links"]) == 1
    link = post_card["links"][0]["url"]
    revision = service._edit_caption(service._post(post_id), f"海底厨房有热饭。 原评论链接：{link}\n原评论：{link}")
    assert revision == "海底厨房有热饭。"
    assert service._outgoing_text(service._post(post_id), revision).count(link) == 1
    card = service.comment_card_image(post_id)
    with Image.open(BytesIO(card)) as rendered:
        assert rendered.format == "PNG" and rendered.width == 960
    observed = []

    async def send(adapter, text, image, *, before_send, prepare_authorized, on_prepared, on_uploaded):
        assert prepare_authorized()
        before_send()
        observed.append((text, image))
        return {"platform": "bilibili", "dynamic_id": "12345"}

    monkeypatch.setattr(module, "publish_image", send)
    text = "前些天有人问起海底厨房，我回他说那里总有一碗热饭。今天揭锅时又想起他这句。"
    service.review(post_id, "approve", text)
    service.store.execute("UPDATE comments SET display_name='改名后的观众' WHERE comment_id='42'")
    result = await service.publish(post_id)
    assert result["status"] == "published"
    assert observed == [(
        text + "\n\n原评论：https://www.bilibili.com/video/BV1d8pU6xEZX#reply42",
        {"mime_type": "image/png", "content": card},
    )]


async def test_public_comment_can_illustrate_its_story_without_losing_the_source_link(service, monkeypatch):
    published_reply(service)
    service.runtime.operation_llm.illustration = "picture"
    generated = {"artifact_id": "a" * 32, "sha256": "b" * 64}
    monkeypatch.setattr(evaluator_module, "workstation_image_decisions", lambda _: True)
    monkeypatch.setattr(attachments_module, "queue_image", lambda *args, **kwargs: generated)
    monkeypatch.setattr(service.account, "display_account_id", lambda: "100", raising=False)
    draft = await service.evaluate(manual=True)
    row = service._post(draft["post_id"])
    assert row["comment_card"] is None
    reviewed = service.review(row["id"], "approve")
    payload = service._outgoing(reviewed, service._character())
    assert payload == {"text": reviewed["content"] + "\n\n原评论：https://www.bilibili.com/video/BV1d8pU6xEZX#reply42",
                       "artifact_id": generated["artifact_id"], "asset_sha256": generated["sha256"]}
    without_illustration = service.review(row["id"], "edit", meme_id=None)
    assert without_illustration["comment_card"] is None
    assert without_illustration["outgoing_links"] == row["outgoing_links"]


async def test_legacy_public_dynamic_reselects_from_saved_actor_and_text(service, monkeypatch):
    published_reply(service)
    monkeypatch.setattr(service.account, "display_account_id", lambda: "100", raising=False)
    service.runtime.operation_llm.illustration = "none"
    legacy = service._post((await service.evaluate(manual=True))["post_id"])
    assert legacy["comment_card"] is None and legacy["generation_identity"].startswith("{")
    received = []

    def queue(runtime, channel, row, context, text, *, comment_card, require_choice):
        assert channel == "dynamics" and text == legacy["content"]
        assert comment_card and require_choice
        return {"job_id": "legacy-reselected"}

    def status(scope, job_id):
        assert scope["account_id"] == "100" and job_id == "legacy-reselected"
        return {"job_id": job_id, "status": "queued"}

    monkeypatch.setattr(attachments_module, "queue_image", queue)
    monkeypatch.setattr(service.runtime, "operation_images", SimpleNamespace(
        status=status, reselect=lambda scope, job_id: received.append(job_id),
    ), raising=False)
    saved = service.review(legacy["id"], "reselect_image", review_key=legacy["review_key"])
    assert received == ["legacy-reselected"] and saved["illustration_choice"] == "awaiting_choice"
    assert saved["content"] == legacy["content"] and saved["source_comment_card"]


def test_old_dynamic_card_preserves_the_original_quote_order_for_reconciliation():
    from tuber_studio.operations.channels.dynamics.comment_card import comment_card

    sources = [{"type": "public_incident", "public_sources": [
        {"bvid": "BV1d8pU6xEZX", "comment_id": "42", "quote": "原来的原话"},
        {"bvid": "BV1d8pU6xEZX", "comment_id": "41", "quote": "之前的原话"},
        {"bvid": "BV1d8pU6xEZX", "comment_id": "42", "quote": "同楼补充引用"},
    ]}]
    card = comment_card(sources, legacy=True)
    assert [item["quote"] for item in card["comments"]] == ["原来的原话", "之前的原话", "同楼补充引用"]
    assert len(card["links"]) == 2


def test_long_card_keeps_ancestors_and_confirmed_outcome_across_pages(service, monkeypatch):
    from PIL import ImageFont

    from tuber_studio.operations.channels.comments.timeline import observe_thread
    from tuber_studio.operations.channels.dynamics import comment_card as dynamic_comment_card
    from tuber_studio.operations.kernel.repository import BrowsingRepository

    monkeypatch.setattr(dynamic_comment_card, "_font", lambda size: ImageFont.load_default())
    store = service.store
    BrowsingRepository(store)
    bvid, root, account = "BV1d8pU6xEZX", "51", "100"
    actor = {"character_id": service._character()["id"], "account_uid": account}
    items = [{"comment_id": str(i), "thread_id": root, "parent_comment_id": str(i - 1) if i > 51 else None,
              "platform_user_id": str(110 + i % 2), "display_name": "观众甲" if i % 2 else "观众乙",
              "content": f"评论原话 {i}", "created_at": i}
             for i in range(51, 59)]
    items.append(items[1] | {"comment_id": "80", "parent_comment_id": root, "content": "无关支线"})
    with store.db:
        observe_thread(store, actor | {"account_id": account, "id": "round-a"}, bvid, items, via="comment_visit")
        store.db.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,kind,status,text,target,receipt,round_id,updated_at) "
            "VALUES('confirmed',?,?,'reply_public','published','确认回复',?,?,'round-a',99)",
            (account, actor["character_id"], json.dumps({"bvid": bvid, "root": root, "parent": "58"}),
             json.dumps({"rpid": "59"})),
        )
        store.db.execute(
            "INSERT INTO browsing_intents(id,account_id,character_id,kind,status,text,target,receipt,round_id,updated_at) "
            "VALUES('unknown',?,?,'reply_public','publish_uncertain','未确认正文',?,NULL,'round-a',100)",
            (account, actor["character_id"], json.dumps({"bvid": bvid, "root": root, "parent": "59"})),
        )
    card = dynamic_comment_card.comment_card([{
        "type": "public_incident", "public_sources": [{"bvid": bvid, "thread_id": root,
                                                        "comment_id": "53", "quote": "评论原话 53"}],
    }], store=store, actor=actor)
    assert card["page_count"] == 2 and card["missing"] == 0
    assert [entry["comment_id"] for entry in card["comments"]] == [str(i) for i in range(51, 60)]
    assert card["comments"][0]["reply_to"] is None
    assert card["comments"][2]["reply_to"] == "观众乙"
    assert card["comments"][-1]["quote"] == "确认回复"
    assert card["comments"][-1]["reply_to"] == "观众乙"
    assert card["links"] == [{"bvid": bvid, "url": f"https://www.bilibili.com/video/{bvid}#reply53"}]
    assert dynamic_comment_card.render_comment_card(card, 0) != dynamic_comment_card.render_comment_card(card, 1)


def test_dynamic_decision_can_merge_episodes(service):
    sources = [{"id": "incident:1"}, {"id": "reply:2"}]
    identifiers = [source["id"] for source in sources]
    assert service.decision_schema(sources)["properties"]["source_ids"]["maxItems"] == 2
    result = service._parse({
        "action": "post", "reason": "想分享", "content": "动态正文。", "source_ids": identifiers,
        "points": [{"text": "两件事的共通点", "source_ids": identifiers}], "next_evaluation_seconds": 300,
    }, sources)
    assert result["points"][0]["source_ids"] == identifiers


async def test_dynamic_refreshes_identity_regenerates_and_keeps_existing_approval(service):
    from tuber_studio.core.identity import identity_snapshot

    public_memory(service)
    char = service._character()
    current = {"account_uid": "injected-test-account", "account_display_name": "旧名字"}
    service.runtime.identity_snapshot = lambda character=None, account_id=None: identity_snapshot(
        current, character or char
    )
    post_id = (await service.evaluate(manual=True))["post_id"]
    previous = json.loads(service._post(post_id)["generation_identity"])
    service.review(post_id, "approve")
    # Identity refresh and scheduled evaluation share the same history boundary.
    checked_caption = "已从私聊清除的公开成品"
    service.store.execute(
        "INSERT INTO dynamic_posts(character_id,character_version,content,sources,status,published_at) "
        "VALUES(?,?,?,?,'published',?)",
        (char["id"], char["version"], checked_caption,
         json.dumps([{"id": "daily-chat:removed", "memory_version": 1}]), time.time()),
    )

    async def refresh(character=None, account_id=None):
        assert not service.account.credential_lock.locked()
        current["account_display_name"] = "新名字"
        return service.runtime.identity_snapshot(character, account_id)

    service.runtime.refresh_identity = refresh
    service.runtime.operation_llm.output = json.dumps(
        {
            "action": "post",
            "reason": "按当前身份分享",
            "content": "我是新名字，海底厨房今天照常开饭。",
            "source_ids": service._post(post_id)["source_ids"],
            "next_evaluation_seconds": 1800,
        },
        ensure_ascii=False,
    )
    result = await service.publish(post_id)
    assert result["status"] == "published"
    assert len(service.sent) == 1
    assert service.sent[0][1] == "我是新名字，海底厨房今天照常开饭。"
    identity = json.loads(service._post(post_id)["generation_identity"])
    assert identity["account_display_name"] == "新名字"
    assert identity["identity_version"] != previous["identity_version"]
    assert "新名字" in service.runtime.operation_llm.calls[-1][0]["content"]
    assert json.loads(service.runtime.operation_llm.calls[-1][1]["content"])["recent_public_posts"] == []


async def test_dynamic_identity_verification_failure_keeps_approved_draft_unsent(service):
    public_memory(service)
    post_id = (await service.evaluate(manual=True))["post_id"]
    service.review(post_id, "approve")
    original = service._post(post_id)["content"]

    async def unavailable(*args):
        raise ValueError("远端资料核验失败")

    service.runtime.refresh_identity = unavailable
    with pytest.raises(ValueError, match="远端资料核验失败"):
        await service.publish(post_id)
    assert service._post(post_id)["status"] == "approved"
    assert service._post(post_id)["content"] == original
    assert not service.sent


async def test_automatic_dynamic_identity_refresh_preserves_automatic_authorization(service):
    from tuber_studio.core.identity import identity_snapshot

    public_memory(service)
    service.account.display_account_id = lambda: "injected-test-account"
    char = service._character()
    current = {"account_uid": "injected-test-account", "account_display_name": "旧名字"}
    service.runtime.identity_snapshot = lambda character=None, account_id=None: identity_snapshot(
        current, character or char
    )

    async def refresh(character=None, account_id=None):
        current["account_display_name"] = "新名字"
        return service.runtime.identity_snapshot(character, account_id)

    service.runtime.refresh_identity = refresh
    await service.set_policy({"enabled": True})
    result = await service.evaluate()
    assert service.list_posts()[0]["error"] is None
    assert result["status"] == "published", (result, service.list_posts())
    assert len(service.sent) == 1
    post = service.list_posts()[0]
    assert json.loads(post["generation_identity"])["account_display_name"] == "新名字"
    decisions = [
        packet
        for packet in service.runtime.operation_llm.calls
        if "task" not in json.loads(packet[1]["content"])
    ]
    assert len(decisions) == 2  # Initial generation and the necessary identity-bound replacement.
    assert "新名字" in decisions[-1][0]["content"]


def test_incident_internal_source_id_cannot_become_public_body():
    with pytest.raises(ValueError, match="内部来源编号"):
        module.validate_text("今天想聊聊 incident:123 里的事。")


async def test_dynamic_can_read_one_page_without_changing_public_source(service, monkeypatch):
    public_memory(service)
    model = service.runtime.operation_llm
    model.supports_operation_web_fetch = True
    original = model.generate_json
    fetched = []
    url = "https://example.org/ocean"

    async def fetch(target):
        fetched.append(target)
        return {"url": target, "title": "海水", "excerpt": "海水含盐。", "truncated": False}

    async def generate(messages, schema, *, max_tokens):
        if schema.get("oneOf", [{}])[-1].get("required") == ["web_fetch"]:
            return {"web_fetch": url}
        return await original(messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(web_fetch, "fetch_excerpt", fetch)
    monkeypatch.setattr(model, "generate_json", generate)
    result = await service.evaluate(manual=True)
    assert result["action"] == "post" and len(result["sources"]) == 1
    assert fetched == [url] and not service.sent
    assert any("海水含盐。" in message["content"] for message in model.calls[-1])


async def test_disabled_default_and_manual_never_auto_sends(service):
    public_memory(service)
    assert service.policy()["enabled"] is False
    assert service.policy()["auto_publish"] is True
    assert (await service.evaluate())["status"] == "skipped"
    assert not service.runtime.operation_llm.calls
    result = await service.evaluate(manual=True)
    assert result["action"] == "post" and result["status"] == "pending_review"
    assert service.sent == []
    await service.set_policy({"enabled": True})
    with pytest.raises(ValueError, match="未授权"):
        await service.publish(result["post_id"], automatic=True)
    due(service)
    await service.set_policy({"auto_publish": False})
    fresh = await service.evaluate()
    assert fresh["action"] == "post" and fresh["post_id"] != result["post_id"]
    assert service.sent == []  # No queue sweep of manual drafts.


async def test_enabled_auto_publishes_only_new_background_decision(service):
    public_memory(service)
    await service.set_policy({"enabled": True})
    result = await service.evaluate()
    assert result["status"] == "published"
    assert len(service.sent) == 1
    post = service.list_posts()[0]
    assert post["receipt"]["dynamic_id"] == "12345" and post["published_at"]
    assert (await service.publish(post["id"]))["receipt"] == post["receipt"]
    assert len(service.sent) == 1


@pytest.mark.parametrize("caption", [
    "海底小窝的灯一亮，我就想把今天的灵感画下来。",
    "我给海底邮局寄了三封信，结果只有一条比目鱼回了明信片。今天这个故事就讲到这儿！",
])
async def test_autonomous_dynamic_publishes_a_thought_or_story_after_drafting(service, caption):
    service.runtime.operation_llm.output = json.dumps({"action": "post", "reason": "分享随想", "content": caption,
                                             "source_ids": [], "next_evaluation_seconds": 1800})
    await service.set_policy({"enabled": True})
    decision = await service.evaluate()
    post = service._post(decision["post_id"])
    assert post["sources"] == [] and post["comment_card"] is None
    assert post["status"] == "published"
    assert [text for _, text in service.sent] == [caption]
    assert len(service.runtime.operation_llm.calls) == 1


@pytest.mark.parametrize(("manual", "with_public"), [(False, False), (True, False), (False, True)])
async def test_memory_and_operations_do_not_disclose_chat(service, manual, with_public):
    from tuber_studio.operations.kernel.repository import BrowsingRepository

    char = service._character()
    account = "injected-test-account"
    scope = (account, char["id"], char["version"])
    runtime, store = service.runtime, service.store
    runtime.memory_organizer = SimpleNamespace(scope=lambda: scope)
    BrowsingRepository(store)
    queued = []
    runtime.browsing = SimpleNamespace(account=account, policy=lambda: {}, sends=SimpleNamespace(
        enqueue=lambda *args: queued.append(args), preparation_slot=lambda *args, **kwargs: nullcontext(),
    ))
    store.execute("CREATE TABLE operator_chat_turns(request_id TEXT PRIMARY KEY, character_id TEXT, "
                  "character_version INTEGER, message TEXT, result TEXT, created_at REAL)")
    turn = str(uuid4())
    original, paraphrase = "我准备换到海边工作。", "吹海风的时候我总想换个地方。"
    store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)", (
        turn, char["id"], char["version"], original,
        json.dumps({"account_id": account, "private": True, "memory_intent": "chat", "reply_text": paraphrase}),
        time.time(),
    ))
    store.execute("CREATE TABLE dynamic_chat_material(turn_id TEXT PRIMARY KEY,material_id TEXT,text TEXT)")
    store.execute("INSERT INTO dynamic_chat_material VALUES(?,?,?)", (turn, "daily-chat:test", paraphrase))
    if with_public:
        public_memory(service)
    await service.set_policy({"enabled": True, "auto_publish": True})
    assert service._memory_preference()[1]["enabled"]
    queries = []
    store.db.set_trace_callback(queries.append)
    try:
        decision = await service.evaluate(manual=manual)
    finally:
        store.db.set_trace_callback(None)
    assert not any("operator_chat_turns" in sql or "dynamic_chat_material" in sql for sql in queries)
    assert runtime.operation_llm.calls
    prompt = json.dumps(runtime.operation_llm.calls, ensure_ascii=False)
    assert all(value not in prompt for value in (original, paraphrase, turn, "daily-chat:test"))
    if with_public:
        assert decision["action"] == "post" and queued == [("dynamics", decision["post_id"])]
        assert decision["sources"][0]["type"] == "public_memory"
    else:
        assert decision["action"] == "wait" and not queued and not service.list_posts()
    assert not service.sent


@pytest.mark.parametrize("origin", ["selected", "source_id_only", "dependency", "context", "manual_topic"])
async def test_legacy_chat_draft_blocked_and_receipts_preserved(service, origin):
    public_memory(service)
    post_id = (await service.evaluate(manual=True))["post_id"]
    post = service.review(post_id, "approve")
    private = {"id": "daily-chat:test", "type": "daily_chat", "text": "已经脱敏的文字"}
    sources, ids, context = post["sources"], post["source_ids"], []
    if origin == "selected":
        sources, ids = [private], [private["id"]]
    elif origin == "source_id_only":
        ids = [private["id"]]
    elif origin == "dependency":
        sources = [sources[0] | {"memory_dependencies": [private]}]
    elif origin == "context":
        sources, ids, context = [], [], [private]
    else:
        topic = {"id": "operator-topic:test", "type": "operator_topic", "text": "号主指定的公开主题"}
        sources, ids, context = [topic], [topic["id"]], [private]
        service.store.execute("UPDATE dynamic_decisions SET sources=? WHERE id=?",
                              (json.dumps(sources), post["decision_id"]))
    service.store.execute("UPDATE dynamic_posts SET sources=?,source_ids=?,context_sources=? WHERE id=?",
                          (json.dumps(sources), json.dumps(ids), json.dumps(context), post_id))
    assert not service._valid_sources(service._post(post_id))
    with pytest.raises(ValueError):
        await service.publish(post_id)
    assert not service.sent

    service.store.execute("UPDATE dynamic_posts SET status='approved' WHERE id=?", (post_id,))
    for status, receipt in [("published", '{"dynamic_id":"12345"}'), ("publish_uncertain", None)]:
        service.store.execute(
            "INSERT INTO dynamic_posts(character_id,character_version,content,sources,status,attempted_at,receipt) "
            "VALUES(?,?,?,?,?,?,?)", (post["character_id"], post["character_version"], "旧正文",
                                      json.dumps([private]), status, time.time(), receipt),
        )
    restored = Dynamics(service.settings, service.store, service.runtime, service.comments)
    assert restored._post(post_id)["status"] == "invalidated"
    with pytest.raises(ValueError):
        restored.review(post_id, "copy")
    assert restored.list_posts("published")[0]["receipt"] == {"dynamic_id": "12345"}
    assert len(restored.list_posts("publish_uncertain")) == 1
    assert restored._recent_public_posts(service._character()) == []
    assert not service.sent


async def test_unselected_memory_revocation_invalidates_an_unsourced_draft(service):
    identifier = public_memory(service)
    service.runtime.operation_llm.output = json.dumps({"action": "post", "reason": "分享随想", "content": "我想画一幅画。",
                                             "source_ids": [], "next_evaluation_seconds": 1800})
    decision = await service.evaluate(manual=True)
    post = service._post(decision["post_id"])
    assert post["sources"] == [] and service._valid_sources(post)
    service.mark_memory(identifier, False)
    assert not service._valid_sources(post)
    assert not service.sent


async def test_automatic_send_rechecks_private_context_before_reserving_write(service, monkeypatch):
    public_memory(service)

    async def send(adapter, text, *, before_send):
        service.store.execute("UPDATE dynamic_posts SET context_sources=? WHERE status='publishing'",
                              (json.dumps([{"id": "daily-chat:old"}]),))
        before_send()
        pytest.fail("不应发起平台写入")

    monkeypatch.setattr(module, "publish_text", send)
    await service.set_policy({"enabled": True})
    await service.evaluate()
    post, = service.list_posts()
    assert post["status"] == "invalidated" and post["attempted_at"] is None
    assert not service.store.all("SELECT token FROM account_writes")


async def test_automatic_dynamic_publishes_caption_and_bound_thread_image(service, monkeypatch):
    draft = published_reply(service)
    service.store.execute(
        "UPDATE comments SET video_id=?,comment_id=?,content=? WHERE id=(SELECT comment_id FROM drafts WHERE id=?)",
        ("BV13S4i6fE82", "323449273328",
         "信图里的，以后我卖你赛博保险。还有养鱼其实很费钱的，你想要会学习，像人的鱼鱼，一周一百左右吧。单纯机器人聊天一天两三块", draft),
    )
    service.store.execute(
        "UPDATE drafts SET text=?,receipt=? WHERE id=?",
        ("哈哈哈！你们看大肥鱼那条，被吐槽半夜薅人起床要token。我笑得停不下来。",
         '{"rpid":"319915188977"}', draft),
    )
    source, = service._sources()
    caption = "评论区有人说以后卖我赛博保险，还认真算起了养鱼成本。坏了，尾巴也得买一份。"
    service.runtime.operation_llm.output = json.dumps({
        "action": "post", "reason": "分享评论区互动", "content": caption,
        "source_ids": [source["id"]], "next_evaluation_seconds": 1800,
        "illustration": "comment_card", "link_source_ids": [source["id"]],
    })
    sent = []

    async def publish_image(adapter, text, image, *, before_send, **kwargs):
        before_send()
        sent.append(text)
        return {"platform": "bilibili", "code": 0, "dynamic_id": "12345"}

    monkeypatch.setattr(module, "publish_image", publish_image)
    await service.set_policy({"enabled": True})
    decision = await service.evaluate()
    assert decision["post_id"], json.dumps(decision, ensure_ascii=False)
    post = service._post(decision["post_id"])
    assert post["content"] == caption
    assert decision["status"] == "published"
    assert len(sent) == 1 and sent[0].startswith(caption) and "#reply323449273328" in sent[0]
    requests = [json.loads(call[1]["content"]) for call in service.runtime.operation_llm.calls]
    assert len([request for request in requests if "character" in request]) == 1
    assert all("sentences" not in request for request in requests)


async def test_old_caption_diagnostic_does_not_obscure_draft_status(service):
    public_memory(service)
    result = await service.evaluate(manual=True)
    diagnostic = "正文中的动作、次数或结果缺少来源支持，保留待审"
    service.store.execute("UPDATE dynamic_posts SET error=? WHERE id=?", (diagnostic, result["post_id"]))
    service.store.execute("UPDATE dynamic_decisions SET error=? WHERE id=?", (diagnostic, result["id"]))
    service.store.set("dynamic_schedule", {"last_error": diagnostic})
    post, = service.list_posts()
    decision, = service.list_decisions()
    assert post["status"] == decision["status"] == "pending_review"
    assert post["error"] is decision["error"] is None
    assert service.policy()["last_error"] is None
    assert service.store.one("SELECT error FROM dynamic_posts")["error"] == diagnostic


async def test_review_mode_and_current_client(service):
    public_memory(service)
    await service.set_policy({"enabled": True, "auto_publish": False})
    result = await service.evaluate()
    with pytest.raises(ValueError, match="审核"):
        await service.publish(result["post_id"])
    service.review(result["post_id"], "approve", "虚构海底剧场：小厨房开饭啦！")
    replacement = object()
    service.account.adapter = replacement
    await service.publish(result["post_id"])
    assert service.sent == [(replacement, "虚构海底剧场：小厨房开饭啦！")]


async def test_only_explicit_public_memory_enters_prompt(service):
    private_id = memory(service, "私聊里尚未公开的秘密ABC")
    memory(service, "用户的私人数据XYZ", role="user")
    memory(service, "未执行的角色计划", kind="role_plan")
    assert (await service.evaluate(manual=True))["action"] == "wait"
    assert json.loads(service.runtime.operation_llm.calls[-1][1]["content"])["public_sources"] == []
    assert [m["id"] for m in service.list_memories()] == [private_id]
    public_memory(service, text="明确允许公开的虚构角色故事")
    await service.evaluate(manual=True)
    prompt = json.dumps(service.runtime.operation_llm.calls, ensure_ascii=False)
    assert "明确允许公开" in prompt and "fictional" in prompt
    assert "秘密ABC" not in prompt and "私人数据XYZ" not in prompt and "未执行的角色计划" not in prompt


async def test_published_reply_includes_original_comment_and_receipt(service):
    rid = published_reply(service)
    decision = await service.evaluate(manual=True)
    assert decision["source_ids"] == [f"reply:{rid}"]
    assert decision["sources"][0]["original_comment"]["text"] == "鱼鱼的海底厨房是什么样？"
    assert decision["sources"][0]["receipt_id"] == "77"
    assert decision["sources"][0]["fictional"] is True


def test_confirmed_dynamic_thread_reply_preserves_both_authors_and_real_permalink(service):
    from tuber_studio.operations.channels.dynamics.materials import dynamic_model_material, source_links
    from tuber_studio.operations.channels.dynamics.turns import _format_dynamic_sources
    from tuber_studio.operations.kernel.repository import BrowsingRepository

    BrowsingRepository(service.store)
    char = service._character()
    service.account.display_account_id = lambda: "100"
    service.runtime.browsing = SimpleNamespace(policy=lambda: {})
    target = {"resource_type": "dynamic", "dynamic_id": "12345", "oid": "76543", "root": "42", "parent": "42",
              "recipient": "215", "recipient_name": "观众", "source_text": "我昨天做了一锅海鲜饭"}
    service.store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,status,text,"
        "target,receipt,updated_at) VALUES('sent','100',?,?,'sent','reply_public','published',?,?,?,1)",
        (char["id"], char["version"], "下次想放点虾仁", json.dumps(target), '{"rpid":"77"}'),
    )
    source, = service._sources()
    public = dynamic_model_material(source)
    assert source_links(public) == ["https://t.bilibili.com/12345#reply42"]
    assert public["source_url"] == "https://t.bilibili.com/12345#reply77"
    rendered = _format_dynamic_sources([public], self_uid="100")
    assert '@观众（UID 215）的评论 42，原话 "我昨天做了一锅海鲜饭"' in rendered
    assert '你已发布的回复 77："下次想放点虾仁"' in rendered
    assert "你已发布的回复" not in _format_dynamic_sources([public], self_uid="101")
    card = service._comment_card([source], {"account_uid": "100", "character_id": char["id"]})
    assert [row["url"] for row in card["comments"]] == [
        "https://t.bilibili.com/12345#reply42", "https://t.bilibili.com/12345#reply77",
    ]
    service.store.execute("UPDATE browsing_intents SET status='publish_uncertain' WHERE id='sent'")
    assert service._sources() == []


@pytest.mark.parametrize("change", ["delete", "unmark", "rewrite", "version", "character"])
async def test_changed_source_or_character_invalidates_before_write(service, change):
    mid = public_memory(service)
    result = await service.evaluate(manual=True)
    service.review(result["post_id"], "approve")
    if change == "delete":
        service.store.execute("DELETE FROM grounded_memory WHERE id=?", (mid,))
    elif change == "unmark":
        service.mark_memory(mid, False)
    elif change == "rewrite":
        service.store.execute("UPDATE grounded_memory SET text='changed' WHERE id=?", (mid,))
    elif change == "version":
        service.store.save_character(service.store.character())
    else:
        other = service.store.save_character(service.store.character() | {"id": "other-dynamic-role"})
        service.store.set("active_character", other["id"])
    with pytest.raises(ValueError, match="来源已变化"):
        await service.publish(result["post_id"])
    assert service.list_posts()[0]["status"] == "invalidated" and not service.sent


async def test_deleted_public_comment_invalidates(service):
    published_reply(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    service.store.execute("DELETE FROM comments")
    with pytest.raises(ValueError):
        await service.publish(decision["post_id"])
    assert not service.sent


@pytest.mark.parametrize(
    "output",
    [
        "```json\n{}\n```",
        "{} trailing",
        "{}",
        '{"action":"post","reason":"r","content":"c","source_ids":["memory:999"],"next_evaluation_seconds":1800}',
        '{"action":"wait","reason":"r","content":"c","source_ids":[],"next_evaluation_seconds":0}',
    ],
)
async def test_invalid_or_ungrounded_model_output_never_posts(service, output):
    public_memory(service)
    service.runtime.operation_llm.output = output
    await service.set_policy({"enabled": True})
    result = await service.evaluate()
    assert result["action"] == "error" and service.sent == [] and service.list_posts() == []
    assert service.policy()["backoff_until"] > time.time()


async def test_generation_holds_unique_runtime_lock_and_character_rechecked(service):
    public_memory(service)

    async def change():
        assert service.runtime.generation_lock.locked()
        service.store.save_character(service.store.character())

    service.runtime.operation_llm.hook = change
    result = await service.evaluate(manual=True)
    assert result["action"] == "error" and not service.list_posts()


async def test_disable_while_model_running_prevents_send(service):
    public_memory(service)
    await service.set_policy({"enabled": True})

    async def disable():
        await service.set_policy({"enabled": False})

    service.runtime.operation_llm.hook = disable
    result = await service.evaluate()
    assert result["status"] == "pending_review" and not service.sent


@pytest.mark.parametrize(
    "failure,status",
    [
        (PublishUncertain("SESSDATA secret"), "publish_uncertain"),
        (RuntimeError("SESSDATA secret"), "publish_uncertain"),
        (ValueError("SESSDATA secret"), "publish_failed"),
    ],
)
async def test_failed_attempts_are_durable_safe_and_not_retried(service, monkeypatch, failure, status):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    calls = []

    async def fail(*args, before_send=None):
        if before_send:
            before_send()
        calls.append(args)
        raise failure

    monkeypatch.setattr(module, "publish_text", fail)
    await service.set_policy({"enabled": True})
    with pytest.raises(ValueError):
        await service.publish(decision["post_id"])
    post = service.list_posts()[0]
    assert post["status"] == status and post["attempted_at"]
    assert "secret" not in json.dumps(service.policy()) + json.dumps(post)
    if status == "publish_uncertain":
        assert service.policy()["enabled"] is True
        assert service.policy()["base"]["enabled"] is True
    again = Dynamics(service.settings, service.store, service.runtime, service.comments)
    with pytest.raises(ValueError):
        await again.publish(post["id"])
    assert len(calls) == 1
    with pytest.raises(ValueError, match="间隔"):
        again._quota()


async def test_inflight_cancel_pauses_and_restart_never_retries(service, monkeypatch):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    started = asyncio.Event()

    async def hang(*args, before_send=None):
        if before_send:
            before_send()
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "publish_text", hang)
    task = asyncio.create_task(service.publish(decision["post_id"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.list_posts()[0]["status"] == "publish_uncertain"
    assert service.policy()["enabled"] is False
    service.store.execute("UPDATE dynamic_posts SET status='publishing'")
    again = Dynamics(service.settings, service.store, service.runtime, service.comments)
    assert again.list_posts()[0]["status"] == "publish_uncertain"


async def test_default_dynamic_frequency(service):
    for index in range(3):
        public_memory(service, text=f"公开虚构故事{index}")
        decision = await service.evaluate(manual=True)
        service.review(decision["post_id"], "approve", f"海底虚构小剧场第{index}回。")
        if index == 1:
            with pytest.raises(ValueError, match="间隔"):
                await service.publish(decision["post_id"])
            service.store.execute(
                "UPDATE dynamic_posts SET attempted_at=? WHERE attempted_at IS NOT NULL",
                (time.time() - 21601,),
            )
        if index == 2:
            service.store.execute(
                "UPDATE dynamic_posts SET attempted_at=? WHERE attempted_at IS NOT NULL",
                (time.time() - 21601,),
            )
            with pytest.raises(ValueError, match="上限"):
                await service.publish(decision["post_id"])
        else:
            await service.publish(decision["post_id"])
    assert len(service.sent) == 2


async def test_normalized_duplicate_reuses_original_delivery(service):
    public_memory(service)
    first = await service.evaluate(manual=True)
    service.review(first["post_id"], "approve", "海底 小剧场！")
    await service.publish(first["post_id"])
    calls = len(service.runtime.operation_llm.calls)
    assert (await service.evaluate(manual=True))["action"] == "post"
    assert len(service.runtime.operation_llm.calls) == calls + 1
    assert json.loads(service.runtime.operation_llm.calls[-1][1]["content"])["public_sources"]
    public_memory(service, text="另一条公开的虚构故事")
    second = await service.evaluate(manual=True)
    service.review(second["post_id"], "approve", "海底小剧场。")
    service.store.execute(
        "UPDATE dynamic_posts SET attempted_at=? WHERE attempted_at IS NOT NULL", (time.time() - 90000,)
    )
    duplicate = await service.publish(second["post_id"])
    assert duplicate["duplicate_of"] == first["post_id"] and duplicate["status"] == "published"
    assert len(service.sent) == 1


async def test_operator_controls_dynamic_quota_and_interval_independently(service):
    await service.set_policy({"max_posts_per_day": 24, "min_post_interval_seconds": 0})
    for _ in range(23):
        service.store.execute("INSERT INTO dynamic_posts(attempted_at) VALUES(?)", (time.time(),))
    service._quota()
    service.store.execute("INSERT INTO dynamic_posts(attempted_at) VALUES(?)", (time.time(),))
    with pytest.raises(ValueError, match="24小时"):
        service._quota()
    await service.set_policy({"max_posts_per_day": 0})
    service._quota()
    await service.set_policy({"min_post_interval_seconds": 15})
    with pytest.raises(ValueError, match="间隔"):
        service._quota()


async def test_publish_shares_comment_lock_and_rechecks_source_after_wait(service):
    mid = public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    async with service.account.credential_lock:
        task = asyncio.create_task(service.publish(decision["post_id"]))
        await asyncio.sleep(0)
        assert not service.sent
        service.mark_memory(mid, False)
    with pytest.raises(ValueError):
        await task
    assert not service.sent


@pytest.mark.parametrize(
    "patch",
    [
        {"evaluate_interval_seconds": 0},
        {"evaluate_interval_seconds": True},
        {"min_post_interval_seconds": -1},
        {"max_posts_per_day": -1},
        {"enabled": 1},
        {"auto_publish": "true"},
        {"csrf": "secret"},
    ],
)
async def test_policy_bounds(service, patch):
    with pytest.raises(ValueError):
        await service.set_policy(patch)


async def test_enable_requires_credentials_and_worker_disabled_by_default(service):
    service.account.credentials_configured = False
    with pytest.raises(ValueError, match="凭证"):
        await service.set_policy({"enabled": True})
    await service.open()
    await asyncio.sleep(0)
    await service.close()
    assert service.runtime.operation_llm.calls == [] and service.sent == [] and service.worker is None


@pytest.mark.parametrize("change", ["stop", "review_mode", "unmark", "character", "account"])
async def test_final_guard_after_signing_await_cancels_without_write(service, monkeypatch, change):
    mid = public_memory(service)
    await service.set_policy({"enabled": True})
    writes = []

    async def preparing(adapter, text, *, before_send):
        # Represents the awaited read-only WBI preparation before the write.
        await asyncio.sleep(0)
        if change == "stop":
            await service.set_policy({"enabled": False})
        elif change == "review_mode":
            await service.set_policy({"auto_publish": False})
        elif change == "unmark":
            service.mark_memory(mid, False)
        elif change == "character":
            service.store.save_character(service.store.character())
        else:
            service.account.adapter = object()
        before_send()
        writes.append(text)
        return {"dynamic_id": "123"}

    monkeypatch.setattr(module, "publish_text", preparing)
    decision = await service.evaluate()
    post = service.list_posts()[0]
    assert decision["status"] in {"pending_review", "invalidated"}
    assert post["attempted_at"] is None and post["status"] != "publish_uncertain"
    assert not writes


@pytest.mark.parametrize("after_lock", [False, True])
async def test_live_priority_checked_before_and_inside_generation_lock(service, after_lock, monkeypatch):
    public_memory(service)
    await service.set_policy({"enabled": True})
    live = service.runtime.live = SimpleNamespace(state="live", busy=False)
    monkeypatch.setattr(service, "is_live_busy", lambda: live.state in {"live", "paused"} and live.busy)
    if after_lock:
        async with service.runtime.generation_lock:
            task = asyncio.create_task(service.evaluate())
            await asyncio.sleep(0)
            live.busy = True
        result = await task
    else:
        live.busy = True
        result = await service.evaluate()
    assert result["action"] == "wait" and "直播" in result["reason"]
    assert not service.runtime.operation_llm.calls and not service.sent
    assert result["next_evaluation_at"] >= result["created_at"] + 300


async def test_public_revoke_while_waiting_for_model_lock_never_enters_prompt(service):
    mid = public_memory(service)
    async with service.runtime.generation_lock:
        task = asyncio.create_task(service.evaluate(manual=True))
        await asyncio.sleep(0)
        service.mark_memory(mid, False)
    result = await task
    assert result["action"] == "wait" and not service.runtime.operation_llm.calls


async def test_distinct_posts_can_reuse_the_same_source(service):
    mid = public_memory(service)
    first = await service.evaluate(manual=True)
    service.store.execute("UPDATE dynamic_posts SET status='invalidated' WHERE id=?", (first["post_id"],))
    second = await service.evaluate(manual=True)
    service.store.execute("UPDATE dynamic_posts SET status='approved' WHERE id=?", (first["post_id"],))
    service.review(second["post_id"], "approve", "另一段虚构海底故事")
    await service.publish(first["post_id"])
    service.store.execute(
        "UPDATE dynamic_posts SET attempted_at=? WHERE id=?", (time.time() - 90000, first["post_id"])
    )
    assert (await service.publish(second["post_id"]))["status"] == "published"
    assert service.list_memories()[0]["id"] == mid and len(service.sent) == 2


def test_memory_id_reuse_new_created_at_needs_new_public_mark(service):
    mid = public_memory(service)
    service.store.execute("UPDATE grounded_memory SET created_at=created_at+1 WHERE id=?", (mid,))
    assert service.list_memories()[0]["public"] is False


async def test_operational_prompt_contains_timezone_and_only_published_history(service):
    service.account.display_account_id = lambda: "injected-test-account"
    public_memory(service)
    first = await service.evaluate(manual=True)
    service.review(first["post_id"], "approve", "已经发布的虚构故事")
    await service.publish(first["post_id"])
    public_memory(service, text="新公开的虚构记忆")
    unverified = await service.evaluate(manual=True)
    service.store.execute(
        "UPDATE dynamic_posts SET account_id=?,status='published',content=? WHERE id=?",
        ("injected-test-account", "状态已改但没有平台回执", unverified["post_id"]),
    )
    await service.evaluate(manual=True)
    payload = json.loads(service.runtime.operation_llm.calls[-1][1]["content"])
    assert payload["current_time"][-6] in {"+", "-"}
    assert payload["recent_public_posts"] == [
        {
            "content": "已经发布的虚构故事",
            "published_at": service._post(first["post_id"])["published_at"],
        }
    ]


async def test_receipt_persistence_failure_is_uncertain_even_for_value_error(service, monkeypatch):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "approve")
    original = service._status

    def fail_success(post_id, status, error=None, receipt=None):
        if status == "published":
            raise ValueError("local storage validation failed")
        return original(post_id, status, error, receipt)

    monkeypatch.setattr(service, "_status", fail_success)
    with pytest.raises(ValueError, match="本地回执待补录"):
        await service.publish(decision["post_id"])
    assert service.list_posts()[0]["status"] == "publish_uncertain"
    assert not service.policy()["enabled"] and len(service.sent) == 1


async def test_dynamic_evaluation_reads_public_story_without_summary(service):
    text = "在虚构的海底厨房，我又盛了一碗饭。"
    mid = public_memory(service, text=text)
    decision = await service.evaluate(manual=True)
    assert decision["status"] == "pending_review"
    assert len(service.runtime.operation_llm.calls) == 1
    packet = json.loads(service.runtime.operation_llm.calls[0][1]["content"])
    assert [(item["id"], item["text"]) for item in packet["public_sources"]] == [(f"memory:{mid}", text)]
    assert service.latest_materials()["sources"][0]["id"] == f"memory:{mid}"
    service.mark_memory(mid, False)
    assert service.latest_materials()["sources"] == []


async def test_revoke_during_dynamic_evaluation_prevents_draft(service):
    mid = public_memory(service)

    async def revoke():
        assert service.runtime.generation_lock.locked()
        service.mark_memory(mid, False)

    service.runtime.operation_llm.hook = revoke
    result = await service.evaluate(manual=True)
    assert result["action"] == "error" and len(service.runtime.operation_llm.calls) == 1
    assert not service.latest_materials()["sources"] and not service.list_posts()


@pytest.mark.parametrize("value", [0, -1, True, "2048"])
async def test_summary_budget_policy_bounds(service, value):
    with pytest.raises(ValueError):
        await service.set_policy({"memory_summary_max_tokens": value})


async def test_posting_keeps_evaluation_sources_and_deduplicates_content(service):
    public_memory(service)
    await service.set_policy({"enabled": True})
    assert (await service.evaluate())["status"] == "published"
    assert service.latest_materials()["sources"]
    due(service)
    assert (await service.evaluate())["action"] == "wait"
    assert len(service.sent) == 1


async def test_decision_grammar_references_only_authorized_sources(service):
    chosen = public_memory(service)
    omitted = public_memory(service, text="已撤销公开授权的角色故事")
    service.mark_memory(omitted, False)
    result = await service.evaluate(manual=True)
    assert result["action"] == "post"
    schema = service.runtime.operation_llm.schemas[-1]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) == {"action", "reason", "content", "source_ids", "points", "next_evaluation_seconds"}
    props = schema["properties"]
    assert props["action"]["enum"] == ["post", "wait"]
    assert props["source_ids"]["items"]["enum"] == [f"memory:{chosen}"]
    assert f"memory:{omitted}" not in props["source_ids"]["items"]["enum"]
    assert props["next_evaluation_seconds"] == {"type": "integer", "minimum": 1, "maximum": MAX_SAFE_INTEGER}


@pytest.mark.parametrize(
    "invalid",
    [
        {"public_sources": []},
        {"action": "wait", "public_sources": []},
        {
            "action": "wait",
            "reason": "r",
            "content": "",
            "source_ids": [],
            "next_evaluation_seconds": 1800,
            "extra": "model metadata",
        },
        "```json\n{}\n```",
        [],
    ],
)
async def test_decision_input_echo_extra_fields_or_non_object_never_publishes(service, monkeypatch, invalid):
    public_memory(service)
    await service.set_policy({"enabled": True})
    original = service.runtime.operation_llm.generate_json

    async def incorrect(messages, schema, *, max_tokens):
        if json.loads(messages[1]["content"]).get("task") == "operations_memory_summary":
            return await original(messages, schema, max_tokens=max_tokens)
        return invalid

    monkeypatch.setattr(service.runtime.operation_llm, "generate_json", incorrect)
    result = await service.evaluate()
    assert result["action"] == "error" and service.list_posts() == [] and service.sent == []


async def test_rejected_draft_does_not_remove_its_source_from_the_next_evaluation(service):
    public_memory(service)
    decision = await service.evaluate(manual=True)
    service.review(decision["post_id"], "reject")
    count = len(service.runtime.operation_llm.calls)
    await service.set_policy({"enabled": True, "auto_publish": False})
    due(service)
    assert (await service.evaluate())["action"] == "post"
    assert len(service.runtime.operation_llm.calls) == count + 1 and service.sent == []
    assert service._post(decision["post_id"])["status"] == "rejected"


async def test_decision_with_internal_source_id_in_body_is_not_a_publishable_draft(service):
    identifier = public_memory(service)
    service.runtime.operation_llm.output = json.dumps(
        {
            "action": "post",
            "reason": "想分享故事",
            "content": f"海底小剧场(memory:{identifier})",
            "source_ids": [f"memory:{identifier}"],
            "next_evaluation_seconds": 1800,
        }
    )
    decision = await service.evaluate(manual=True)
    assert decision["action"] == "error" and service.list_posts() == [] and service.sent == []


async def test_dual_track_source_post_autopopulates_links_and_allows_pure_thought(service):
    from tuber_studio.persona.memory.daily import DailyMemoryService

    memory_svc = DailyMemoryService(service.store)
    char = service._character()
    ids = []
    for idx in range(3):
        identifier = memory_svc.record_public_incident(
            char["id"],
            char["version"],
            incident_key=f"inc-{idx}",
            highlight_date="2026-11-08",
            headline=f"公开事件{idx}",
            anecdote=f"观众在评论区提了第{idx}个梗。",
            public_sources=[
                {
                    "bvid": "BV13S4i6fE82",
                    "thread_id": str(100 + idx),
                    "comment_id": str(200 + idx),
                    "quote": f"原评第{idx}句",
                }
            ],
        )
        ids.append(f"incident:{identifier}")

    # Track 1: citing linkable sources without explicit link_source_ids auto-fills up to 2 links
    service.runtime.operation_llm.output = json.dumps(
        {
            "action": "post",
            "reason": "整理今天评论区几个好玩的吐槽",
            "content": "今天评论区连续几个吐槽挺有意思，顺手记一笔。",
            "source_ids": ids,
            "next_evaluation_seconds": 1800,
        }
    )
    decision = await service.evaluate(manual=True)
    post = service._post(decision["post_id"])
    assert post["presentation"]["link_source_ids"] == ids[:2]

    # Track 2: pure thought with source_ids=[] succeeds cleanly without links
    service.runtime.operation_llm.output = json.dumps(
        {
            "action": "post",
            "reason": "单纯发个即时感悟",
            "content": "夜里突然想到一件事：写代码跟烧水一样，越盯着看越慢。",
            "source_ids": [],
            "next_evaluation_seconds": 1800,
        }
    )
    thought_decision = await service.evaluate(manual=True)
    thought_post = service._post(thought_decision["post_id"])
    assert thought_post["source_ids"] == []
    assert thought_post["presentation"]["link_source_ids"] == []


async def test_zero_candidate_mention_demotes_while_ambiguous_mention_blocks_approve(service):
    public_memory(service)

    class MentionLookupAdapter:
        async def mention_accounts(self, query):
            if query == "同名路人":
                return {"71": "同名路人", "81": "同名路人"}
            return {}

    service.account.adapter = MentionLookupAdapter()

    # Zero-candidate unresolvable @mention is demoted cleanly and does NOT block approval
    service.runtime.operation_llm.output = json.dumps(
        {
            "action": "post",
            "reason": "顺手提到一个没查到UID的人",
            "content": "刚才看见 @不存在的路人 说的那套说辞真是离谱。",
            "source_ids": [],
            "mentions": [{"label": "@不存在的路人", "account": "不存在的路人"}],
            "next_evaluation_seconds": 1800,
        }
    )
    demoted_decision = await service.evaluate(manual=True)
    demoted_post = service._post(demoted_decision["post_id"])
    assert demoted_post["presentation"]["pending_mentions"] == []
    assert len(demoted_post["presentation"]["demoted_mentions"]) == 1
    approved = service.review(demoted_post["id"], "approve")
    assert approved["status"] == "approved"

    # Candidate-bearing ambiguous @mention stays in pending_mentions and blocks approve
    service.runtime.operation_llm.output = json.dumps(
        {
            "action": "post",
            "reason": "重名候选需要号主确认",
            "content": "点名感谢 @同名路人 的提醒。",
            "source_ids": [],
            "mentions": [{"label": "@同名路人", "account": "同名路人"}],
            "next_evaluation_seconds": 1800,
        }
    )
    ambiguous_decision = await service.evaluate(manual=True)
    ambiguous_post = service._post(ambiguous_decision["post_id"])
    assert len(ambiguous_post["presentation"]["pending_mentions"]) == 1
    assert len(ambiguous_post["presentation"]["pending_mentions"][0]["candidates"]) == 2
    with pytest.raises(ValueError, match="请先选择这条动态要 @ 的账号"):
        service.review(ambiguous_post["id"], "approve")
