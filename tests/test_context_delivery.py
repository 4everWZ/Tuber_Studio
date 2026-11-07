"""Old draft text and unverifiable summaries stay auditable, outside model recall."""

import json
import time

import pytest
from test_comments import comments as comments

from tuber_studio.context import ContextManager
from tuber_studio.context_delivery import comment_delivery, memory_source_allowed
from tuber_studio.runtime import Runtime


def original_and_draft(comments, *, status="published", receipt=None, suffix="1"):
    store, char = comments.store, comments.runtime.character
    comments.import_items("import-delivery", [{"comment_id": suffix, "thread_id": "root",
        "platform_user_id": "neighbor", "content": f"保留的原观众问题{suffix}"}])
    source = store.one("SELECT * FROM comments WHERE comment_id=?", (suffix,))
    text = f"我去了银月书店{suffix}，店主小壳送我三本诗集。"
    identity = store.execute("INSERT INTO drafts(comment_id,text,status,character_id,character_version,account_id,receipt) VALUES(?,?,?,?,?,'100',?)",
        (source["id"], text, status, char["id"], char["version"], json.dumps(receipt) if receipt else None)).lastrowid
    scope = comments.account_context_scope(source["video_id"], source["thread_id"], account_id="100")
    event = comments._comment_event(source) | {"account_id": "100"}
    comments.runtime.context.record(scope, char, event, text, "comment_draft")
    return scope, source, event, text, identity


@pytest.mark.parametrize("status,receipt,allowed", [
    ("pending_review", None, False), ("approved", {"rpid": "900"}, False),
    ("rejected", {"rpid": "900"}, False), ("copied", {"rpid": "900"}, False),
    ("publish_uncertain", {"rpid": "900"}, False), ("published", None, False),
    ("published", {"rpid": "0"}, False), ("published", {"code": -1, "rpid": "900"}, False),
    ("published", {"rpid": "900"}, True),
])
def test_grounded_recall_and_history_require_matching_published_receipt(comments, status, receipt, allowed):
    scope, _, event, text, _ = original_and_draft(comments, status=status, receipt=receipt)
    runtime, char = comments.runtime, comments.runtime.character
    recalled = runtime.context.memory.retrieve(scope, char, "上次银月书店是谁送了你三本诗集？")
    assert bool(recalled["evidence"]) is allowed
    assert bool([item for item in runtime.context.history_for_model(scope, char) if item["role"] == "assistant"]) is allowed
    assert text in runtime.context.existing(scope, char)["history"]
    assert comments.store.one("SELECT text FROM grounded_memory WHERE event_id=? AND role='assistant'", (event["event_id"],))["text"] == text


def test_receipt_does_not_authorize_other_account_floor_source_or_changed_body(comments):
    scope, _, event, text, _ = original_and_draft(comments, receipt={"rpid": "900"})
    char, store = comments.runtime.character, comments.store
    assert comment_delivery(store, scope, char, event["event_id"], text)
    for other in (comments.context_scope("import-delivery", "root", account_id="200"),
                  comments.context_scope("import-delivery", "another-floor", account_id="100"),
                  'comments:["bilibili","import-delivery","root"]'):
        assert comment_delivery(store, other, char, event["event_id"], text) is None
    assert comment_delivery(store, scope, char, "comment:99999", text) is None
    assert comment_delivery(store, scope, char, event["event_id"], text + "篡改") is None
    row = store.one("SELECT * FROM grounded_memory WHERE role='assistant'")
    assert not memory_source_allowed(store, row, account_id="200")


async def test_runtime_retrieve_before_prepare_excludes_rejected_source(comments, monkeypatch):
    import tuber_studio.runtime as runtime_module

    scope, source, _, text, _ = original_and_draft(comments, status="rejected", suffix="bad")
    runtime = comments.runtime
    monkeypatch.setattr(runtime, "generate_reply", Runtime.generate_reply.__get__(runtime, Runtime))
    event = comments._comment_event(source) | {"event_id": "new-followup", "account_id": "100",
                                               "text": "上次银月书店是谁送了你三本诗集？"}
    captured = []
    recovery_contexts = []
    original = runtime.llm.generate_operation
    original_recovery = runtime_module.generate_with_recovery

    async def recovery(*args, **kwargs):
        recovery_contexts.append(json.dumps(kwargs["context_data"], ensure_ascii=False))
        return await original_recovery(*args, **kwargs)

    async def generate(messages, character, event_id, source_text=None):
        captured.append(json.dumps(messages, ensure_ascii=False))
        return await original(messages, character, event_id, source_text)

    monkeypatch.setattr(runtime.llm, "generate_operation", generate)
    monkeypatch.setattr(runtime_module, "generate_with_recovery", recovery)
    async with runtime.offline_generation():
        _, audit = await runtime.generate_reply(scope, runtime.character, event, mode="comment_draft")
    assert audit["sources"] == [] and text not in captured[0]
    assert recovery_contexts and all(text not in packet for packet in recovery_contexts)
    assert text in runtime.context.existing(scope, runtime.character)["history"]


async def test_mixed_old_summary_is_never_resummarized_and_original_audit_survives(comments):
    scope, _, _, delivered, draft_id = original_and_draft(comments, receipt={"rpid": "901"})
    _, _, _, unsent, _ = original_and_draft(comments, status="rejected", suffix="bad")
    runtime, store, char = comments.runtime, comments.store, comments.runtime.character
    row = runtime.context.existing(scope, char)
    raw = json.loads(row["history"]) + [{"role": "user", "kind": "summary", "content": "旧污染摘要：" + unsent}]
    store.execute("UPDATE context_windows SET history=? WHERE key=?", (json.dumps(raw, ensure_ascii=False), row["key"]))

    class Summarizer:
        server_tokens = 131072
        profile_error = None
        calls = []

        async def count_tokens(self, messages):
            return 100

        async def summarize(self, messages, *, max_tokens):
            self.calls.append(json.dumps(messages, ensure_ascii=False))
            return "我实际发过：" + delivered

    provider = Summarizer()
    manager = ContextManager(comments.settings, store, provider)
    await manager.compact(scope, char, "comment_draft")
    assert provider.calls and all(unsent not in call for call in provider.calls)
    assert delivered in provider.calls[0]
    summaries = [item for item in manager.history_for_model(scope, char) if item.get("kind") == "summary"]
    assert len(summaries) == 1 and summaries[0]["delivery_provenance"]["sources"]
    archived = store.one("SELECT history FROM context_history_audit WHERE window_key=?", (row["key"],))
    assert archived and unsent in archived["history"]
    store.execute("UPDATE drafts SET status='rejected' WHERE id=?", (draft_id,))
    assert not [item for item in manager.history_for_model(scope, char) if item.get("kind") == "summary"]


def test_context_audit_expires_with_parent_or_disappears_on_explicit_window_delete(comments):
    scope, _, _, _, _ = original_and_draft(comments, status="rejected")
    manager, store, char = comments.runtime.context, comments.store, comments.runtime.character
    row = manager.existing(scope, char)
    manager._archive_history(row, "test old pollution")
    store.execute("UPDATE context_windows SET oldest_source_at=? WHERE key=?", (1, row["key"]))
    from tuber_studio.retention import RetentionManager

    retention = RetentionManager(comments.settings, store)
    preview = retention.preview(now=time.time())
    assert preview["counts"]["context_history_audit"] == 1
    retention.cleanup(now=time.time())
    assert manager.existing(scope, char) is None and not store.all("SELECT * FROM context_history_audit")
    assert manager.memory.retrieve(scope, char, "银月书店三本诗集")["evidence"] == []
    replacement = manager.window(scope, char)
    manager._archive_history(replacement, "manual delete audit")
    store.execute("DELETE FROM context_windows WHERE key=?", (replacement["key"],))
    assert not store.all("SELECT * FROM context_history_audit")


def test_profile_clear_or_opt_out_never_reintroduces_unsent_memory(comments):
    from tuber_studio.relationships import UserProfile

    scope, _, _, _, _ = original_and_draft(comments, status="rejected")
    runtime, char = comments.runtime, comments.runtime.character
    runtime.relationships.set_profile("100", char["id"], "bilibili", "neighbor",
                                     profile=UserProfile(personalization_enabled=False, nickname="", preferences=[], important_events=[]))
    assert runtime.context.memory.retrieve(scope, char, "你刚才去银月书店了吗")["evidence"] == []
    runtime.relationships.set_profile("100", char["id"], "bilibili", "neighbor")
    assert runtime.context.memory.retrieve(scope, char, "你刚才去银月书店了吗")["evidence"] == []


def test_dynamic_public_mark_and_cached_summary_do_not_launder_an_unsent_memory(comments, monkeypatch):
    from tuber_studio.dynamics import Dynamics
    from tuber_studio.operation_identity import identity_snapshot

    _, _, _, text, _ = original_and_draft(comments, status="rejected")
    runtime, store, char = comments.runtime, comments.store, comments.runtime.character
    monkeypatch.setattr(runtime, "identity_snapshot", lambda character=None, account_id=None:
                        identity_snapshot({"account_uid": "100"}, character or char))
    dynamics = Dynamics(comments.settings, store, runtime, comments)
    source = store.one("SELECT * FROM grounded_memory WHERE role='assistant'")
    store.execute("INSERT INTO dynamic_memory_public VALUES(?,?,?)", (source["id"], dynamics._memory_fingerprint(source), time.time()))
    old_source = {"id": f"memory:{source['id']}", "type": "public_memory", "fictional": True, "text": text}
    store.execute("INSERT INTO dynamic_memory_summaries VALUES(?,?,?,?,?,?,?,?)",
                  (char["id"], char["version"], dynamics.memory_summary.signature([old_source]),
                   dynamics.policy()["memory_summary_max_tokens"], 100, json.dumps([{"source_id": old_source["id"], "quote": text}]),
                   time.time(), dynamics._summary_model()))
    assert dynamics._memory_rows() == []
    assert old_source["id"] not in {source["id"] for source in dynamics._sources()}
    assert dynamics.summary_status()["valid"] is False and dynamics.summary_status()["entries"] == []
    assert store.one("SELECT text FROM grounded_memory WHERE id=?", (source["id"],))["text"] == text
