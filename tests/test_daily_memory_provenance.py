"""Daily summaries retain verified source quotes without laundering draft text."""

import datetime
import json

from tuber_studio.daily_memory import DailyMemoryService


def original(store):
    return store.execute("INSERT INTO comments(video_id,comment_id,content,imported_at) VALUES('import-proof','123',?,strftime('%s','now'))",
                         ("观众说鲸鱼游泳像一碗热白饭",)).lastrowid


def highlight(service, store, *, quote, source_evidence=None):
    return service.record_highlight("whale", 1, datetime.date.today().isoformat(), "comment_anecdote",
        "热白饭的比喻", quote, "有人把鲸鱼游泳比作一碗热白饭。", source_ref="video:import-proof:comment:123",
        tags=["热白饭"], source_evidence=source_evidence)


def test_unpublished_assistant_quote_is_auditable_but_never_daily_recall(store):
    service = DailyMemoryService(store)
    source = original(store)
    draft_text = "我去了幽灵饭馆，店主给我未发送的白饭。"
    store.execute("INSERT INTO drafts(comment_id,text,status) VALUES(?,?,'rejected')", (source, draft_text))
    identifier = highlight(service, store, quote=draft_text)
    assert service.get_active_anecdotes("whale", 1, query_text="热白饭") == []
    assert service.recall_historical_highlight("whale", 1, "你还记得热白饭吗") is None
    assert service.list_highlights("whale", 1)[0]["id"] == identifier
    assert store.one("SELECT quote FROM daily_highlights WHERE id=?", (identifier,))["quote"] == draft_text
    assert service.candidate_sources(datetime.date.today().isoformat(), "whale")[0]["content"] != draft_text


def test_legacy_viewer_quote_is_backfilled_and_retained_after_original_cleanup(store):
    service = DailyMemoryService(store)
    source = original(store)
    quote = store.one("SELECT content FROM comments WHERE id=?", (source,))["content"]
    identifier = highlight(service, store, quote=quote)
    store.execute("UPDATE daily_highlights SET source_evidence=NULL WHERE id=?", (identifier,))
    service = DailyMemoryService(store)
    proof = json.loads(store.one("SELECT source_evidence FROM daily_highlights WHERE id=?", (identifier,))["source_evidence"])
    assert proof["kind"] == "viewer" and proof["confirmed_by"] == "source_match" and proof["quote"] == quote
    store.execute("DELETE FROM comments WHERE id=?", (source,))
    recalled = service.get_active_anecdotes("whale", 1, query_text="热白饭")
    assert recalled[0]["id"] == identifier and recalled[0]["source_role"] == "viewer"


async def test_auto_approve_distillation_keeps_viewer_proof_instead_of_operator_note(store):
    service = DailyMemoryService(store)
    source = original(store)
    quote = store.one("SELECT content FROM comments WHERE id=?", (source,))["content"]

    class Extractor:
        async def count_tokens(self, messages):
            return 100

        async def generate_json(self, messages, schema, *, max_tokens):
            return {"highlights": [{"category": "comment_anecdote", "headline": "热白饭的比喻",
                "source_ref": "video:import-proof:comment:123", "quote": quote,
                "anecdote": "有人把鲸鱼游泳比作一碗热白饭。", "tags": ["热白饭"]}]}

    values = await service.distill(store.character(), datetime.date.today().isoformat(), Extractor(), auto_approve=True)
    assert len(values) == 1
    row = store.one("SELECT * FROM daily_highlights WHERE id=?", (values[0]["id"],))
    proof = json.loads(row["source_evidence"])
    assert row["status"] == "active" and proof["kind"] == "viewer" and proof["confirmed_by"] != "operator"
    store.execute("DELETE FROM comments WHERE id=?", (source,))
    assert service.recall_historical_highlight("whale", 1, "还记得热白饭吗")["source_role"] == "viewer"


def test_only_explicit_operator_confirmation_can_admit_an_unverifiable_note(store):
    service = DailyMemoryService(store)
    identifier = highlight(service, store, quote="管理员确认的一则虚构角色日常")
    assert service.get_active_anecdotes("whale", 1) == []
    service.update_highlight(identifier, {"status": "active"})
    assert service.get_active_anecdotes("whale", 1)[0]["source_role"] == "operator_note"
