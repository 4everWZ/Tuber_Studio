import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import ImageFont
from test_dynamics import published_reply
from test_dynamics import service as service

from tuber_studio import dynamic_comment_card, dynamics
from tuber_studio.agent.dynamic_publication import ExistingDraft, named_in_goal, register_dynamic_publication
from tuber_studio.agent.task_runtime import TaskRuntime
from tuber_studio.platforms.bilibili import PublishUncertain


@pytest.fixture
async def handoff(service, monkeypatch):
    account = "100"
    character = service._character()
    scope = [account, character["id"], character["version"], "edit"]
    service.comments.display_account_id = lambda: account

    async def account_id():
        return account

    service.comments.account_id = account_id
    monkeypatch.setattr(dynamic_comment_card, "_font", lambda size: ImageFont.load_default())
    published_reply(service)
    draft = (await service.evaluate(manual=True))["post_id"]

    def scope_fn():
        return scope.copy()

    tasks = TaskRuntime(service.store, scope_fn, None)
    register_dynamic_publication(tasks, service)
    adapter = SimpleNamespace()
    service.comments.adapter = adapter
    outcome = {"disconnect": False, "visible": False}
    sends, reads = [], []

    async def send(actual_adapter, text, image, *, before_send, prepare_authorized, on_prepared, on_uploaded):
        assert actual_adapter is adapter and image["content"] and prepare_authorized()
        before_send()
        sends.append(text)
        if outcome["disconnect"]:
            raise PublishUncertain("lost response")
        return {"dynamic_id": "991"}

    async def reconcile(target, *, author_uid, text, started_at, receipt):
        reads.append((target["post_id"], author_uid, text))
        return {"status": "visible", "receipt": {"dynamic_id": "991"}} if outcome["visible"] else {"status": "ambiguous"}

    adapter.reconcile_dynamic_publication = reconcile
    monkeypatch.setattr(dynamics, "publish_image", send)
    task_id = str(uuid4())
    task = tasks.create(task_id, f"请发布动态草稿 ID {draft}")
    step = {"tool": "publish_dynamic_draft", "id": str(uuid4()), "call_id": "publish", "response_id": "turn",
            "arguments": {"post_id": draft}, "revision": 1}
    tasks._save(task, plan={"backend": "responses", "prior_goals": []}, steps=[step])
    await tasks._advance(tasks.current(task_id))
    task = tasks.current(task_id)
    assert task["status"] == "awaiting_approval" and not sends
    approved = task["steps"][0]["approval"]
    assert approved["binding"]["account_id"] == account and approved["revision"] == service._post(draft)["updated_at"]
    assert approved["binding"]["character"] == {"id": scope[1], "version": scope[2]}
    assert "UID 100" in approved["target"] and "原评论：https://" in approved["text"]
    assert approved["sources"][0]["receipt_id"] == "77"
    await tasks.control(task_id, 1, "approve", step_id=step["id"])
    assert service._post(draft)["status"] == "pending_review" and not sends
    return SimpleNamespace(**locals())


async def test_original_receipt_only_after_lost_response(handoff):
    data = handoff
    data.outcome["disconnect"] = True
    await data.tasks._advance(data.tasks.current(data.task_id))
    assert data.sends == [data.approved["text"]] and not data.reads
    assert data.service._post(data.draft)["status"] == "publish_uncertain"
    claim = data.tasks.current(data.task_id)["steps"][0]["checkpoint"]
    assert claim["status"] == "needs_reconciliation"
    recovered = TaskRuntime(data.service.store, data.scope_fn, None)
    register_dynamic_publication(recovered, data.service)
    for visible in (False, True):
        data.outcome["visible"] = visible
        task = recovered.current(data.task_id)
        recovered._save(task, steps=[task["steps"][0] | {"due": 0}])
        await recovered._advance(recovered.current(data.task_id))
        assert data.sends == [data.approved["text"]]
        assert data.reads[-1] == (data.draft, "100", data.approved["text"])
        if not visible:
            assert recovered.current(data.task_id)["status"] == "waiting"
    result = recovered.current(data.task_id)["steps"][0]["result"]
    assert result["status"] == "published" and result["receipt"] == {"dynamic_id": "991"}
    assert data.service.operations.get_attempt(result["write_token"])["resolved_status"] == "published"
    assert len(data.reads) == 2


@pytest.mark.parametrize("case", ["edit", "wrong_account", "lost_before_send", "lost_after_send", "foreign_step"])
async def test_approval_never_resends_or_inherits_an_unbound_receipt(handoff, monkeypatch, case):
    data = handoff
    service = data.service
    if case == "edit":
        service.review(data.draft, "edit", data.approved["draft_text"])
    elif case == "wrong_account":
        async def changed():
            return "101"
        service.comments.account_id = changed
    elif case.startswith("lost"):
        publish = service.publish
        async def interrupt(*args, **kwargs):
            if case == "lost_after_send":
                await publish(*args, **kwargs)
            raise asyncio.CancelledError
        monkeypatch.setattr(service, "publish", interrupt)
        with pytest.raises(asyncio.CancelledError):
            # Simulate a dead worker after the durable claim and before its result is written.
            task = data.tasks.current(data.task_id)
            await data.tasks._invoke(task, task["steps"][0], [], task["goal"])
    else:
        data.outcome["disconnect"] = True
        await data.tasks._advance(data.tasks.current(data.task_id))
        saved = json.loads(service.store.one("SELECT send_payload FROM dynamic_posts WHERE id=?", (data.draft,))["send_payload"])
        saved["agent_step"] = str(uuid4())
        service.store.execute("UPDATE dynamic_posts SET send_payload=? WHERE id=?", (json.dumps(saved), data.draft))
        task = data.tasks.current(data.task_id)
        data.tasks._save(task, steps=[task["steps"][0] | {"due": 0}])
    await data.tasks._advance(data.tasks.current(data.task_id))
    step = data.tasks.current(data.task_id)["steps"][0]
    assert len(data.sends) == (1 if case in {"lost_after_send", "foreign_step"} else 0)
    assert not data.reads
    if case == "lost_after_send":
        assert step["result"]["receipt"] == {"dynamic_id": "991"}
    else:
        assert step["checkpoint"]["status"] == "needs_reconciliation" and "result" not in step


@pytest.mark.parametrize("goal", ["发布最新的动态", "向 UID 42 发布动态", "动态草稿 420", "草稿 #42 和草稿 #43"])
def test_model_cannot_select_an_unspecified_draft(goal):
    assert not named_in_goal(goal, ExistingDraft(post_id=42))


async def test_revoke_at_transport_boundary_does_not_reserve_or_send(handoff, monkeypatch):
    data = handoff

    async def revoke(adapter, text, image, *, before_send, prepare_authorized, on_prepared, on_uploaded):
        assert prepare_authorized()
        data.scope[-1] = "different-identity"
        before_send()
        pytest.fail("publication continued after revocation")

    monkeypatch.setattr(dynamics, "publish_image", revoke)
    await data.tasks._advance(data.tasks.current(data.task_id))
    assert data.service.store.one("SELECT COUNT(*) AS n FROM account_writes")["n"] == 0
    assert data.service._post(data.draft)["status"] == "pending_review"
    archived = data.tasks._decode(data.service.store.one("SELECT * FROM agent_tasks WHERE id=?", (data.task_id,)))
    assert "待核对" in data.tasks.view(archived)["message"]


async def test_private_topic_and_unbound_draft_are_not_disclosed(handoff):
    data = handoff
    tool = data.tasks.tools["inspect_dynamic_draft"]
    args = ExistingDraft(post_id=data.draft)
    observed = await tool.run(None, args)
    assert observed["sources"] == data.approved["sources"]
    data.service.store.execute("UPDATE dynamic_posts SET account_id='' WHERE id=?", (data.draft,))
    with pytest.raises(ValueError, match="账号"):
        await tool.run(None, args)
    topic = json.dumps([{"id": "operator-topic:1", "type": "operator_topic", "text": "private"}])
    decision = data.service._post(data.draft)["decision_id"]
    data.service.store.execute("UPDATE dynamic_decisions SET sources=? WHERE id=?", (topic, decision))
    data.service.store.execute("UPDATE dynamic_posts SET account_id='100',sources=? WHERE id=?", (topic, data.draft))
    with pytest.raises(ValueError, match="公开出处"):
        await tool.run(None, args)
