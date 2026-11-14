"""Evidence-gated workflows with real SQLite, cache and task cancellation."""

import asyncio
import json
import re
import time

import httpx
import pytest

from tuber_studio.comments import Comments
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.dynamics import Dynamics
from tuber_studio.operations.browsing.models import BrowsingPolicy
from tuber_studio.operations.browsing.service import BrowsingService

BV = "BV1d8pU6xEZX"


def model_packet(source):
    body = source.split("：", 1)[1]
    if not body.startswith("{"):
        header, content = body.split("\n内容:\n", 1)
        value = json.loads(header.rsplit("\n", 1)[-1])
        value["metadata"] = {"title": header.split("\n", 1)[0].removeprefix("标题:")}
        value["content"] = content
        value["segments"] = [{"id": reference} for reference in re.findall(r"(?m)^\[(e\d+)\|", content)]
        return value
    if "\n\n所选材料：\n" in body:
        header, content = body.split("\n\n所选材料：\n", 1)
        value = json.loads(header)
        value["evidence"] = [
            {"id": reference} for reference in re.findall(r"(?m)^\[(ref:\d+)(?:\||\])", content)
        ]
        value["content"] = content
        return value
    header, separator, content = body.partition("\n\n内容：\n")
    value = json.loads(header)
    if separator:
        value["content"] = content
        value["segments"] = [{"id": reference} for reference in re.findall(r"(?m)^\[(e\d+)\|", content)]
    return value


def batch_plan_wire(choices, *, workers=1, explanation=""):
    assert len({item["bvid"] for item in choices}) == len(choices)
    return {
        "workers": workers,
        "explanation": explanation,
        "choices": {
            item["bvid"]: {key: value for key, value in item.items() if key != "bvid"}
            | {"priority": len(choices) - index}
            for index, item in enumerate(choices)
        },
    }


class ControlledPlatform:
    def __init__(self):
        self.account = "100"
        self.calls = []
        self.sent = []
        self.feed = {"mention": [], "reply": []}
        self.no_subtitles = False
        self.delay = 0
        self.description = "完整的盐水实验简介"
        self.failure = None
        self.before_submit = None

    async def close(self):
        pass

    async def self_uid(self):
        return self.account

    async def discover(self, source, keyword="", page=1, limit=20):
        self.calls.append(("discover", source, keyword, page))
        return {
            "items": [{"bvid": BV, "aid": 42, "title": "盐水实验", "owner_uid": "200", "duration": 30}],
            "has_more": False,
        }

    async def video_metadata(self, video):
        self.calls.append(("metadata", video))
        await asyncio.sleep(self.delay)
        return {
            "bvid": video,
            "aid": 42,
            "title": "盐水实验",
            "description": self.description,
            "tags": ["科普"],
            "owner_uid": "200",
            "owner": "盐水实验室",
            "duration": 30,
            "parts": [{"cid": "11", "title": "第一P", "duration": 30}],
        }

    async def subtitles(self, bvid, cid, languages):
        self.calls.append(("subtitles", bvid, cid))
        if self.no_subtitles:
            from tuber_studio.operations.browsing.evidence import EvidenceUnavailable

            raise EvidenceUnavailable("no_subtitles", "无CC字幕")
        await asyncio.sleep(self.delay)
        return {
            "bvid": bvid,
            "cid": cid,
            "track_id": "track",
            "language": "zh-CN",
            "is_ai": True,
            "segments": [{"id": "0", "start": 0, "end": 10, "text": "加盐之后，鸡蛋浮了起来。"}],
            "coverage": {"complete": True},
        }

    async def fetch(self, video, max_pages=1):
        return {"video_id": video, "items": []}

    async def notifications(self, kind, cursor=None):
        return {"account_id": self.account, "items": self.feed[kind], "has_more": False, "cursor": None}

    async def read_public_thread(self, event):
        return {
            "account_id": self.account,
            "resource_type": "video",
            "target": {"type": 1, "oid": "42", "bvid": BV, "root": "501", "parent": "502"},
            "source": {"comment_id": "502", "author_uid": "300", "text": "大肥鱼你怎么看盐水里的鸡蛋？"},
            "items": [],
            "public_text": "",
        }

    async def publish_top_level(self, target, text, *, before_send):
        if self.before_submit:
            await self.before_submit()
        before_send()
        self.sent.append((target, text))
        if self.failure:
            raise self.failure
        return {"code": 0, "rpid": "900"}

    async def publish_resource_reply(self, target, text, *, before_send):
        return await self.publish_top_level(target, text, before_send=before_send)

    async def like_video(self, target, *, like=True, before_send=None):
        if before_send:
            before_send()
        self.sent.append((target, "like" if like else "unlike"))
        return {"code": 0, "action": "like" if like else "unlike", "video_id": target.get("bvid")}

    async def favorite_video(self, target, *, folder_id=None, before_send=None):
        if before_send:
            before_send()
        self.sent.append((target, "favorite"))
        return {"code": 0, "action": "favorite", "video_id": target.get("bvid")}

    async def verify_publication(self, target, receipt):
        return {"status": "not_found"}


class ControlledModel:
    def __init__(self):
        self.calls = []
        self.active = self.peak = 0
        self.interest = "like"
        self.worth = True
        self.response_action = "reply_public"
        self.bad_ref = False

    async def health(self):
        return {"ok": True}

    async def close(self):
        pass

    async def generate(self, messages, character, event_id, source_text=None):
        from tuber_studio.models import Reply

        return Reply(
            spoken_text="鱼鱼在这里陪你聊，我们慢慢说。",
            emotion="cheerful",
            animation_id="talk",
            reply_to=[event_id],
        )

    async def count_tokens(self, messages):
        return sum(len(m["content"]) for m in messages) // 2 + 1

    async def generate_json(self, messages, schema, *, max_tokens):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.001)
            if schema["title"] == "OutreachAssessment":
                data = json.loads(messages[-1]["content"])
                self.calls.append((schema["title"], data))
                return {"action": "send", "reason": "对方邀请我方就具体实验私下继续", "purpose": data["purpose"] or "讨论盐水鸡蛋实验",
                    "invitation_kind": "invitation", "invitation_comment_id": data["public_thread"]["source"]["comment_id"],
                    "invitation_quote": data["public_thread"]["source"]["text"], "invited_actor_uid": data["self_account_uid"], "body_addresses_purpose": True}
            data = model_packet(messages[-1]["content"])
            self.calls.append((schema["title"], data))
            if schema["title"] == "BrowsePlan":
                return {"action": "recommend", "keywords": [], "reason": "explore", "delay_seconds": 300}
            if schema["title"] == "CandidateBatchPlan":
                return batch_plan_wire(
                    [
                        {
                            "bvid": item["bvid"],
                            "action": "watch" if index < data["max_videos"] else "skip",
                            "explanation": f"想看看{item['title']}的具体过程",
                        }
                        for index, item in enumerate(data["candidates"])
                    ], workers=data["max_workers"], explanation="看看这批视频里各自的实验结果",
                )
            if schema["title"] == "ContentDecision":
                return {
                    "interest": self.interest,
                    "worth_commenting": self.worth,
                    "reason": "specific_detail" if self.worth else "nothing_to_add",
                    "explanation": "加盐后鸡蛋浮起来这个细节想聊聊"
                    if self.worth
                    else "盐水实验看明白了，暂时没有补充",
                    "evidence_ids": ["missing"] if self.bad_ref else [data["segments"][0]["id"]],
                    "coverage_required": "provided",
                }
            if schema["title"] == "VideoEngagementDecision":
                return {
                    "should_like": True,
                    "like_reason": "视频很有趣味，值得点赞",
                    "should_favorite": True,
                    "favorite_reason": "优质干货，收藏备查",
                }
            action = self.response_action
            if data.get("video_actions") and not data.get("allow_public", True):
                action = "finish"
            return {
                "action": action,
                "text": "盐水把鸡蛋托起来这段好有意思，鱼鱼也想试试！" if action != "finish" else "",
                "reason": "specific_detail" if action in {"reply_public", "finish"} else "user_requested",
                "explanation": "想接着聊盐水如何把鸡蛋托起来",
                "evidence_ids": [data["evidence"][0]["id"]],
                **({"contact_purpose": "讨论盐水鸡蛋实验", "contact_trigger": "invitation"} if action == "handoff_private" else {}),
                **(
                    {
                        "video_preference": self.interest,
                        "preference_reason": "盐水密度改变后鸡蛋浮起来的过程很有趣",
                    }
                    if data.get("video_actions")
                    else {}
                ),
            }
        finally:
            self.active -= 1


@pytest.fixture
async def browsing(settings, store, runtime):
    adapter = ControlledPlatform()
    comments = Comments(settings, store, runtime, adapter)

    class MessageTransport:
        async def sessions(self):
            return "100", []

        async def close(self):
            pass

    messages = DirectMessages(settings, store, runtime, comments, MessageTransport())
    dynamics = Dynamics(settings, store, runtime, comments)
    comments.direct_messages = messages
    service = BrowsingService(settings, store, runtime, comments, messages, dynamics, read_interval=0)
    runtime.browsing = service
    previous = runtime.llm
    runtime.llm = ControlledModel()
    settings.data["llm"]["kind"] = "llama_cpp"
    service.store.set("browsing_policy", BrowsingPolicy(top_level_enabled=True).model_dump())
    yield service
    await service.close()
    await comments.close()
    runtime.llm = previous


async def authorize_automation(service):
    """Explicit test permissions; starting the parent never adds capabilities."""
    service.store.set("browsing_policy", service.store.get("browsing_policy", {}) | {
        "discovery_enabled": True,
        "top_level_enabled": True,
        "top_level_auto_publish": True,
        "reply_enabled": True,
        "reply_auto_publish": True,
    })
    await service.set_enabled(True)


async def start_automation(service):
    await service.open()
    await authorize_automation(service)
    return service.status()


async def finished(service, **args):
    if args.get("automatic") and not service.operations.policy()["enabled"]:
        await authorize_automation(service)
    result = await service.run(**args)
    task = service.active.get(result["id"])
    if task:
        await asyncio.wait_for(task, 3)
    return service.repo.round(result["id"])


async def wait_for_sends(service):
    async with asyncio.timeout(2):
        while service.sends.active or service.store.one(
            "SELECT 1 FROM operation_send_queue WHERE wake_at<=? LIMIT 1", (time.time(),)
        ):
            await asyncio.sleep(0.005)


async def test_discovery_batch_has_no_hidden_shared_request_allowance(browsing):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({"max_model_calls": 1, "decision_max_tokens": 512})
    await browsing._current_account()

    first = await finished(browsing, automatic=True, video_only=True)
    assert not browsing.repo.video_retry(first, BV)
    assert {task["status"] for task in browsing.tasks(round_id=first["id"])} == {"selected"}
    assert browsing.store.one("SELECT bvid FROM browsing_batch_comment_pages")["bvid"] == BV
    await wait_for_sends(browsing)
    assert first["config"]["max_model_calls"] == 0
    assert first["budget"]["model_calls"] == 2
    assert [name for name, _ in browsing.runtime.llm.calls] == [
        "CandidateBatchPlan",
        "ResponseDecision",
    ]
    assert len(browsing.adapter.sent) == 1
    replies = await finished(browsing, automatic=True, interaction_only=True)
    assert replies["status"] == "completed"
    assert not browsing.tasks(round_id=replies["id"])


async def test_video_preview_leaves_authorized_inbox_to_its_own_round(browsing, monkeypatch):
    await browsing.preflight()
    browsing.operations.configure(
        {"enabled": True}, account_id=browsing.account, character=browsing.runtime.character
    )

    async def unexpectedly_processed(*args, **kwargs):
        pytest.fail("a preview must not consume the authorized inbox")

    monkeypatch.setattr(browsing, "_process_events", unexpectedly_processed)
    assert (await finished(browsing, preview=True, video=BV))["status"] == "completed"
    assert not browsing.adapter.sent


async def test_interaction_round_replies_to_own_upload_while_discovery_reads(browsing, monkeypatch):
    own_video = "BV1xx411c7mD"
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure({
        "reply_auto_publish": True, "reply_enabled": True,
        "replies_enabled": True, "recent_sample_limit": 0,
    })
    browsing.store.set(
        "browsing_semantic_validation", {"passed": True, "signature": browsing.model_signature()}
    )
    await browsing._current_account()
    browsing.ready_signature = browsing.credential_signature()
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    await browsing.sync_notifications()
    other = {"event_id": "other", "kind": "reply", "sender_uid": "301", "sender_name": "邻居"}
    browsing.store.execute(
        "INSERT INTO browsing_events VALUES('other','100','reply','802',?,'received',?,NULL,?)",
        (time.time(), json.dumps(other), time.time()),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    metadata = browsing.adapter.video_metadata

    async def hold_video(video):
        entered.set()
        await release.wait()
        return await metadata(video)

    async def own_uploads(account):
        assert account == "100"
        return [{"bvid": own_video, "aid": "43"}]

    async def fetch(video, max_pages=1, *, newest=False):
        if video != own_video:
            return {"items": []}
        assert newest
        return {
            "items": [
                {
                    "comment_id": "801",
                    "thread_id": "801",
                    "platform_user_id": "300",
                    "display_name": "观众",
                    "content": "想知道这期视频怎么拍的？",
                    "created_at": str(int(time.time())),
                }
            ]
        }

    async def read_public_thread(event):
        if event["kind"] == "reply":
            return {
                "account_id": "100",
                "resource_type": "dynamic",
                "public_text": "另一条公开动态",
                "target": {"type": 17, "oid": "44", "root": "701", "parent": "802"},
                "source": {"comment_id": "802", "author_uid": "301", "text": "来聊下盐水实验吧"},
                "items": [],
            }
        assert event["kind"] == "own_comment"
        return {
            "account_id": "100",
            "resource_type": "video",
            "public_text": "自己发布的新视频",
            "target": {"type": 1, "oid": "43", "bvid": own_video, "root": "801", "parent": "801"},
            "source": {"comment_id": "801", "author_uid": "300", "text": event["source_text"]},
            "items": [],
        }

    monkeypatch.setattr(browsing.adapter, "video_metadata", hold_video)
    monkeypatch.setattr(browsing.adapter, "own_uploads", own_uploads, raising=False)
    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_public_thread", read_public_thread)
    await start_automation(browsing)
    async with asyncio.timeout(3):
        await entered.wait()
        reading = browsing.discovery.round_id
        assert reading is not None
        row = await finished(browsing, automatic=True, interaction_only=True)
        assert row["status"] == "completed" and row["id"] != reading, row
        assert browsing.repo.round(reading)["status"] == "running"
        await wait_for_sends(browsing)
        assert [target["parent"] for target, _ in browsing.adapter.sent] == ["802", "801"], (
            browsing.store.all("SELECT id,status,error FROM browsing_events")
        )
        await browsing.stop(emergency=True)
        assert not browsing.discovery.enabled and browsing.repo.round(reading)["status"] == "paused"
    release.set()
    await browsing.set_enabled(False)


@pytest.mark.parametrize(("workers", "cancel"), [(1, False), (2, False), (2, True)])
async def test_background_discovery_uses_shared_worker_limit(browsing, monkeypatch, workers, cancel):
    videos = [BV, "BV1xx411c7mD", "BV17x411w7KC"]
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure(
        {
            "workers": workers,
            "sources": ["specified"],
            "watch_videos": videos,
            "recent_sample_limit": 0,
            "decision_max_tokens": 512,
        }
    )
    reading = set()
    peak = 0
    full, release = asyncio.Event(), asyncio.Event()
    metadata = browsing.adapter.video_metadata

    async def track_metadata(video):
        nonlocal peak
        reading.add(video)
        peak = max(peak, len(reading))
        if peak == workers:
            full.set()
        try:
            await release.wait()
            return await metadata(video)
        finally:
            reading.remove(video)

    monkeypatch.setattr(browsing.adapter, "video_metadata", track_metadata)
    await start_automation(browsing)
    async with asyncio.timeout(3):
        await full.wait()
        assert len(reading) == workers
        if cancel:
            await browsing.stop(emergency=True)
            assert not reading and not browsing.active and not browsing.heartbeats
            assert next(
                row for row in browsing.rounds() if not row["config"].get("interaction_only")
            )["status"] == "paused"
            assert not browsing.adapter.sent
            return
        active = next(iter(browsing.active.values()))
        release.set()
        await active
    await browsing.set_enabled(False)
    assert peak == workers
    assert browsing.runtime.llm.peak == 1
    assert {task["bvid"] for task in browsing.tasks() if task["status"] == "selected"} == set(videos)


async def test_background_discovery_publishes_top_level_and_thread_replies(browsing, monkeypatch):
    second_video = "BV1xx411c7mD"
    policy = browsing.policy
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    monkeypatch.setattr(
        browsing,
        "policy",
        lambda: (
            policy()
            | {
                "interval_seconds": 0.03,
                "top_level_enabled": True,
                "top_level_auto_publish": True,
                "reply_enabled": True,
                "recent_sample_limit": 0,
                "max_replies_per_video": 0,
            }
        ),
    )

    async def fetch(video, max_pages=1):
        if video != BV:
            return {"items": []}
        return {
            "items": [
                {
                    "comment_id": "502",
                    "thread_id": "501",
                    "content": "有意思的盐水实验",
                    "display_name": "测试观众",
                    "platform_user_id": "300",
                }
            ]
        }

    async def read_reply(target):
        assert target["root"] == "501" and target["parent"] == "502"
        return {"author_uid": "300", "text": "有意思的盐水实验"}

    async def discover(source, keyword, page, limit):
        assert source == "recommend" and not keyword
        return {
            "items": [
                {"bvid": second_video if browsing.intents() else BV, "title": "盐水实验", "duration": 30}
            ],
            "has_more": False,
        }

    async def notifications(*args):
        raise AssertionError("独立刷视频不读取通知")

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.adapter, "notifications", notifications)
    monkeypatch.setattr(browsing.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing.adapter, "read_video_reply", read_reply, raising=False)
    await browsing.open()
    assert (await start_automation(browsing))["policy"]["enabled"]
    worker = browsing.loop
    await start_automation(browsing)
    assert browsing.loop is worker
    async with asyncio.timeout(5):
        while sum(item["status"] == "published" for item in browsing.intents()) < 3:
            await asyncio.sleep(0.01)
    assert browsing.discovery.enabled
    await browsing.set_enabled(False)
    if draining := browsing.drain_task:
        await draining

    rows = browsing.rounds()
    video_rows = [row for row in rows if not row["config"].get("interaction_only")]
    assert len(video_rows) >= 2 and all(row["config"].get("autonomous_batch") for row in video_rows)
    assert {row["bvid"] for row in browsing.tasks() if row["status"] == "selected"} == {BV, second_video}
    assert all(row["status"] == "published" and row["auto_authorized"] for row in browsing.intents())
    assert not browsing.operations.policy()["enabled"]
    assert sorted(target["parent"] for target, _ in browsing.adapter.sent) == ["0", "0", "502"]
    assert browsing.adapter.sent[0][0]["parent"] == "0"
    assert browsing.discovery.status()["state"] == "off"
    activity = browsing.discovery.records(recent=True)["activity"]
    assert {item["kind"] for item in activity if item["status"] == "published"} == {
        "top_level",
        "reply_public",
    }
    assert all(
        item["title"] == "盐水实验" and item["excerpt"] for item in activity if item["status"] == "published"
    )


async def test_discovery_source_changes_at_the_next_round(browsing, monkeypatch):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    prepare = browsing._prepare_evidence
    started, resume = asyncio.Event(), asyncio.Event()

    async def prepare_video(*args, **kwargs):
        started.set()
        await resume.wait()
        return await prepare(*args, **kwargs)

    monkeypatch.setattr(browsing, "_prepare_evidence", prepare_video)
    await browsing.open()
    await start_automation(browsing)
    await asyncio.wait_for(started.wait(), 3)
    before = browsing.discovery.status()
    assert before["state"] == "reading" and before["current_video"]["title"] == "盐水实验"
    assert before["summary"] == {"processed": 0, "skipped": 0}
    edited = await browsing.discovery.configure_source(
        {"mode": "search", "keywords": [" 盐水 "], "watch_videos": []}
    )
    assert edited["source"]["mode"] == "search"
    assert edited["round_source"] == "recommend"
    assert edited["active_round"]["id"] == before["active_round"]["id"]
    policy = browsing.policy
    monkeypatch.setattr(browsing, "policy", lambda: policy() | {"interval_seconds": 0.03})
    resume.set()
    async with asyncio.timeout(5):
        while ("discover", "search", "盐水", 1) not in browsing.adapter.calls:
            await asyncio.sleep(0.01)
    await browsing.set_enabled(False)
    assert {round_["config"]["sources"][0] for round_ in browsing.rounds()} == {"recommend", "search"}


@pytest.mark.parametrize("failed_reads", [1, 3])
async def test_discovery_defers_failed_attempt_then_continues_other_videos(browsing, monkeypatch, failed_reads):
    from tuber_studio.platforms.bilibili_browsing import PlatformReadError

    first, second = BV, "BV1xx411c7mD"
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    base = browsing.policy
    monkeypatch.setattr(
        browsing,
        "policy",
        lambda: (
            base()
            | {
                "interval_seconds": 0.03,
                "recent_sample_limit": 0,
                "workers": 1,
            }
        ),
    )
    finish_attempt = browsing.repo.finish_video_attempt

    def finish_without_wall_clock_wait(row, candidate, task_id, failures):
        finish_attempt(row, candidate, task_id, failures)
        if 0 < failures < 3:
            for table, key in (("browsing_tasks", "id"), ("browsing_batch_items", "task_id")):
                browsing.store.execute(f"UPDATE {table} SET payload=json_set(payload,'$.next_at',0) WHERE {key}=?", (task_id,))

    monkeypatch.setattr(browsing.repo, "finish_video_attempt", finish_without_wall_clock_wait)
    events = []
    prepare = browsing._prepare_evidence
    publish = browsing.publish
    finish_publication = asyncio.Event()

    async def publish_video(identity, *, automatic=False):
        result = await publish(identity, automatic=automatic)
        intent = next(item for item in browsing.intents() if item["id"] == identity)
        if intent["target"]["bvid"] == first:
            # A confirmed send can precede the video task's final recovery checkpoint.
            await finish_publication.wait()
        return result

    async def prepare_video(video, *args, **kwargs):
        events.append(video)
        if video == first and events.count(first) <= failed_reads:
            raise PlatformReadError("network", "读取超时")
        return await prepare(video, *args, **kwargs)

    async def discover(*args):
        return {"items": [{"bvid": video} for video in (first, second)], "has_more": False}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing, "_prepare_evidence", prepare_video)
    monkeypatch.setattr(browsing, "publish", publish_video)
    await browsing.open()
    await start_automation(browsing)
    async with asyncio.timeout(5):
        while not any(target["bvid"] == second for target, _ in browsing.adapter.sent) or (
            failed_reads == 3 and not any(task.get("consecutive_failures") == 3 for task in browsing.tasks())
        ) or (
            failed_reads == 1 and not any(target["bvid"] == first for target, _ in browsing.adapter.sent)
        ):
            await asyncio.sleep(0.01)
    await browsing.set_enabled(False)
    finish_publication.set()
    if draining := browsing.drain_task:
        await asyncio.wait_for(draining, 5)
    assert events[:3] == [first, second, first]
    if failed_reads == 3:
        assert events[3] == first
        assert not any(target["bvid"] == first for target, _ in browsing.adapter.sent)
        assert any(
            task["status"] == "skipped" and task.get("consecutive_failures") == 3 for task in browsing.tasks()
        )
    else:
        assert any(target["bvid"] == first for target, _ in browsing.adapter.sent)
    activity = browsing.discovery.records(recent=True)["activity"]
    failures = [item for item in activity if item["kind"] == "failure" and item["bvid"] == first]
    assert len(failures) == 1
    assert failures[0]["occurrences"] == failed_reads
    assert failures[0]["error"]
    assert failures[0]["status"] == ("retry_wait" if failed_reads == 3 else "recovered")


async def test_discovery_unknown_receipt_only_reads_original_and_keeps_publishing(browsing, monkeypatch):
    from tuber_studio.platforms.bilibili import PublishUncertain

    first, second = BV, "BV1xx411c7mD"
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    base = browsing.policy
    monkeypatch.setattr(
        browsing,
        "policy",
        lambda: (
            base()
            | {
                "interval_seconds": 0.03,
                "recent_sample_limit": 0,
                "workers": 1,
            }
        ),
    )
    actions = []

    async def discover(*args):
        return {"items": [{"bvid": video} for video in (first, second)], "has_more": False}

    async def publish(target, text, *, before_send):
        before_send()
        actions.append(("write", target["bvid"]))
        if target["bvid"] == first:
            raise PublishUncertain("发送响应超时")
        return {"code": 0, "rpid": "900"}

    async def reconcile(target, **options):
        actions.append(("lookup", target["oid"]))
        return {"status": "not_found"}

    monkeypatch.setattr(browsing.adapter, "discover", discover)
    monkeypatch.setattr(browsing.adapter, "publish_top_level", publish)
    monkeypatch.setattr(browsing.adapter, "reconcile_publication", reconcile, raising=False)
    await browsing.open()
    await start_automation(browsing)
    for _ in range(300):
        if ("write", second) in actions and ("lookup", "42") in actions:
            break
        await asyncio.sleep(0.01)
    assert ("write", second) in actions and ("lookup", "42") in actions, (
        actions, browsing.tasks(), browsing.rounds(), browsing.last_error
    )
    await browsing.set_enabled(False)
    assert [action for action in actions if action[0] == "write"] == [("write", first), ("write", second)]
    assert all(action == ("lookup", "42") for action in actions if action[0] == "lookup")
    assert not browsing.operations.status()["channel_blocks"]
    assert {row["status"] for row in browsing.intents()} == {"publish_uncertain", "published"}
    activity = browsing.discovery.records(recent=True)["activity"]
    assert {item["status"] for item in activity} >= {"publish_uncertain", "published"}


async def test_discovery_agent_revises_definite_rejection_and_resubmits(browsing, monkeypatch):
    from tuber_studio.operation_feedback import FeedbackModel
    from tuber_studio.publication_errors import PlatformRejected

    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    texts = []
    published = asyncio.Event()

    async def publish(target, text, *, before_send):
        before_send()
        texts.append(text)
        if len(texts) == 1:
            raise PlatformRejected(12051, "请修改正文")
        published.set()
        return {"code": 0, "rpid": "901"}

    async def decide(self, row):
        assert row["trigger_feedback"]["code"] == 12051
        return {"action": "revise", "reason": "按照平台反馈修订"}

    async def revise(self, row, decision):
        return "原来盐水的密度会影响鸡蛋浮沉，鱼鱼记住啦。"

    monkeypatch.setattr(browsing.adapter, "publish_top_level", publish)
    monkeypatch.setattr(FeedbackModel, "decide", decide)
    monkeypatch.setattr(FeedbackModel, "revise", revise)
    await browsing.open()
    await start_automation(browsing)
    async with asyncio.timeout(5):
        await published.wait()
        await wait_for_sends(browsing)
        await browsing.set_enabled(False)
    assert len(texts) == 2 and texts[0] != texts[1]
    records = browsing.discovery.records()
    assert records["sent"][0]["receipt_id"] == "901"
    assert records["issues"] == []
    continuation = browsing.intents()[0]["continuation"]
    assert continuation["state"] == "completed"


async def test_emergency_during_discovery_preflight_cancels_start(browsing, monkeypatch):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    started = asyncio.Event()

    async def wait_for_model():
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(browsing, "check_browsing_model", wait_for_model)
    starting = asyncio.create_task(start_automation(browsing))
    await asyncio.wait_for(started.wait(), 2)
    await browsing.stop(emergency=True)
    starting.cancel()
    await asyncio.gather(starting, return_exceptions=True)
    assert not browsing.discovery.enabled and not browsing.rounds()
    with pytest.raises(ValueError, match="紧急停止"):
        await start_automation(browsing)
    browsing.operations.configure({"emergency": False})
    assert browsing.discovery.status()["state"] == "off"


async def test_preview_complete_evidence_review_and_one_shot(browsing):
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed", row
    intent = browsing.intents()[0]
    assert intent["status"] == "pending_review" and not intent["auto_authorized"]
    assert intent["evidence"] == ["11:0"] and browsing.adapter.sent == []
    browsing.review(intent["id"], "approve")
    result = await browsing.publish(intent["id"])
    assert result["status"] == "published" and result["visibility"] == "not_found"
    assert browsing.adapter.sent[0][0]["root"] == "0"
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    assert len(browsing.adapter.sent) == 1


async def test_review_edit_preserves_status_and_updates_text(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    assert intent["status"] == "pending_review"
    edited = browsing.review(intent["id"], "edit", "这是大肥鱼人工修改后的精彩台词")
    assert edited["status"] == "pending_review"
    assert edited["text"] == "这是大肥鱼人工修改后的精彩台词"
    assert not edited["auto_authorized"]
    # Still requires explicit approval before publish
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    # Approve and publish
    browsing.review(intent["id"], "approve")
    published = await browsing.publish(intent["id"])
    assert published["status"] == "published"
    assert published["text"] == "这是大肥鱼人工修改后的精彩台词"


async def test_missing_cc_never_uses_title_or_comments(browsing):
    browsing.adapter.no_subtitles = True
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed"
    assert browsing.tasks()[0]["reason"] == "no_subtitles"
    assert not browsing.runtime.llm.calls and not browsing.intents()


async def test_content_decision_requests_required_sources_before_generation(browsing):
    captured = []
    generate = browsing.runtime.llm.generate_json

    async def observe(messages, schema, *, max_tokens):
        if schema["title"] == "ContentDecision":
            captured.append(schema)
        return await generate(messages, schema, max_tokens=max_tokens)

    browsing.runtime.llm.generate_json = observe
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed" and browsing.intents()
    branches = {branch["properties"]["worth_commenting"]["const"]: branch for branch in captured[0]["oneOf"]}
    for branch in branches.values():
        assert "evidence_ids" in branch["required"]
        assert branch["properties"]["evidence_ids"]["items"]["enum"] == ["e1"]
    assert branches[True]["properties"]["evidence_ids"]["minItems"] == 1
    assert branches[False]["properties"]["evidence_ids"]["minItems"] == 0


@pytest.mark.parametrize("entrypoint", ["video", "comment"])
async def test_browsing_reads_comments_through_real_adapter_fetch(browsing, entrypoint):
    from tuber_studio.platforms.bilibili_browsing import BrowsingAdapter

    requests = []
    text = "我也想试试这个盐水实验"

    def handle(request):
        requests.append(request)
        if request.url.path == "/x/web-interface/view":
            data = {"bvid": BV, "aid": 42, "title": "盐水实验"}
        elif request.url.path == "/x/v2/reply":
            data = {
                "replies": [
                    {
                        "rpid": 501,
                        "member": {"mid": "200", "uname": "观众"},
                        "content": {"message": text},
                    }
                ],
                "page": {"count": 1},
            }
        else:
            raise AssertionError(request.url)
        return httpx.Response(200, json={"code": 0, "data": data})

    adapter = BrowsingAdapter()
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handle)
    )
    adapter._read_spacing = 0
    browsing.adapter.fetch = adapter.fetch
    try:
        if entrypoint == "video":
            row = await finished(browsing, preview=True)
            assert row["status"] == "completed", row
            assert browsing.intents()[0]["status"] == "pending_review"
        else:
            browsing.comments.import_items(
                BV,
                [{"comment_id": "501", "platform_user_id": "200", "content": text}],
            )
            comment = browsing.store.one("SELECT * FROM comments")
            result = await browsing.comment_evidence(comment, browsing.runtime.character)
            assert result["segments"]
        assert [request.url.path for request in requests] == [
            "/x/web-interface/view",
            "/x/v2/reply",
        ]
        assert all(request.method == "GET" for request in requests)
        assert not browsing.adapter.sent
    finally:
        await adapter.close()


async def test_evidence_workers_inherit_parent_budget_and_deadline(browsing, monkeypatch):
    original, received = browsing.evidence.prepare, []

    async def observe(video, config, **options):
        received.append(options)
        return await original(video, config, **options)

    monkeypatch.setattr(browsing.evidence, "prepare", observe)
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed" and received
    assert all(option["task"]["round_id"] == row["id"] for option in received)
    assert all(option["task"]["id"] for option in received)
    assert all(option["deadline"] <= row["deadline"] for option in received)


async def test_visual_evidence_cannot_charge_a_different_parent_round(browsing):
    from tuber_studio.operations.browsing.round_context import read_round

    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    token = read_round.set(row["id"])
    try:
        with pytest.raises(ValueError, match="父轮预算"):
            await browsing._prepare_evidence(BV, browsing.policy(), task={"round_id": "other-round"})
    finally:
        read_round.reset(token)
    assert not browsing.adapter.calls


async def test_invalid_citation_cannot_create_candidate(browsing):
    browsing.runtime.llm.bad_ref = True
    row = await finished(browsing, preview=True)
    assert row["status"] == "partial" and not browsing.intents()


async def test_liking_does_not_require_comment(browsing):
    browsing.runtime.llm.worth = False
    await finished(browsing, preview=True)
    assert browsing.tasks()[0]["status"] == "viewed_without_comment"
    assert not browsing.intents()


async def test_no_keywords_is_recommend_and_never_invented_search(browsing):
    await finished(browsing, preview=True)
    calls = [c for c in browsing.adapter.calls if c[0] == "discover"]
    assert calls and all(c[1:3] == ("recommend", "") for c in calls)


async def test_user_keywords_preserved_and_sources_deduplicated(browsing):
    browsing.store.set("browsing_policy", BrowsingPolicy(keywords=["盐水", "鸡蛋"]).model_dump())
    await finished(browsing, preview=True)
    assert len([c for c in browsing.adapter.calls if c[0] == "metadata"]) == 1
    assert {c[2] for c in browsing.adapter.calls if c[0] == "discover"} == {"盐水", "鸡蛋"}


async def test_parent_cancel_collects_workers_and_preserves_no_send(browsing):
    browsing.adapter.delay = 0.2
    result = await browsing.run(preview=True)
    await asyncio.sleep(0.02)
    await browsing.stop()
    assert browsing.repo.round(result["id"])["status"] == "cancelled"
    assert not browsing.active and not browsing.evidence.pending and not browsing.heartbeats
    assert not browsing.adapter.sent


async def test_rejected_top_level_stays_deduplicated_after_reread(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "reject")
    result = await browsing.reread(browsing.tasks()[0]["id"])
    await browsing.active[result["id"]]
    assert len(browsing.intents()) == 1 and browsing.intents()[0]["status"] == "rejected"


async def test_shared_quota_and_uncertain_block_new_auto_writes(browsing):
    from tuber_studio.platforms.bilibili import PublishUncertain

    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    browsing.adapter.failure = PublishUncertain("timeout")
    with pytest.raises(PublishUncertain):
        await browsing.publish(intent["id"])
    assert browsing.intents()[0]["status"] == "publish_uncertain"
    assert browsing.store.one("SELECT status FROM account_writes")["status"] == "uncertain"
    with pytest.raises(ValueError):
        await browsing.publish(intent["id"])
    assert len(browsing.adapter.sent) == 1


@pytest.mark.parametrize("expired", [False, True])
async def test_source_change_invalidates_reviewed_reply(browsing, expired):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    if expired:
        browsing.store.execute("UPDATE browsing_materials SET expires_at=0")
    browsing.adapter.description = "原始视频说明已修改"
    with pytest.raises(ValueError, match="材料已变化"):
        await browsing.publish(intent["id"])
    assert not browsing.adapter.sent


async def test_adapter_replacement_cannot_publish_with_old_account_credentials(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    previous = browsing.adapter
    replacement = ControlledPlatform()
    replacement.account = "200"
    browsing.comments.adapter = replacement
    with pytest.raises(ValueError, match="账号已变化"):
        await browsing.publish(intent["id"])
    assert not previous.sent and not replacement.sent


async def test_expired_evidence_is_checked_and_reused_for_manual_publish(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")
    browsing.store.execute("UPDATE browsing_materials SET expires_at=0")
    result = await browsing.publish(intent["id"])
    assert result["status"] == "published"
    assert result["material_keys"] == intent["material_keys"]
    assert browsing.evidence.load(result["material_keys"][0], allow_expired=True)


async def test_emergency_during_preflight_to_http_means_no_submit(browsing):
    await finished(browsing, preview=True)
    intent = browsing.intents()[0]
    browsing.review(intent["id"], "approve")

    async def change():
        browsing.store.activate("whale") if False else None
        browsing.comments.credentials["csrf"] = "changed"

    browsing.adapter.before_submit = change
    with pytest.raises(ValueError, match="账号"):
        await browsing.publish(intent["id"])
    assert not browsing.adapter.sent


async def test_per_account_round_owner_is_shared(browsing):
    browsing.adapter.delay = 0.05
    a, b = await asyncio.gather(browsing.run(preview=True), browsing.run(preview=True))
    assert a["id"] == b["id"]
    await browsing.active[a["id"]]


async def test_lease_takeover_fences_old_results(browsing):
    repo = browsing.repo
    row, _ = repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    task = repo.task(row["id"], {"bvid": BV})
    first = repo.claim(task, "first")
    browsing.store.execute("UPDATE browsing_tasks SET lease_until=? WHERE id=?", (time.time() - 1, task))
    second = repo.claim(task, "second")
    with pytest.raises(ValueError, match="旧Worker"):
        repo.complete(task, "first", first, "selected", {})
    repo.complete(task, "second", second, "viewed_without_comment", {})
    assert second == first + 1


async def test_atomic_budget_cannot_expand_on_parallel_reservations(browsing):
    config = browsing.policy() | {"max_model_calls": 1}
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, config, 0, True)
    browsing.repo.reserve_budget(row["id"], model_calls=1)
    with pytest.raises(ValueError, match="预算耗尽"):
        browsing.repo.reserve_budget(row["id"], model_calls=1)
    assert browsing.repo.round(row["id"])["budget"]["model_calls"] == 1


async def test_model_not_semantically_validated_retains_review(browsing):
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    await finished(browsing, automatic=True)
    assert browsing.intents()[0]["status"] == "pending_review"
    assert not browsing.status()["automatic_sending_ready"] and not browsing.adapter.sent


async def test_notification_baseline_then_alias_dedup_not_video_seen_dedup(browsing):
    config = BrowsingPolicy(
        mentions_enabled=True, mention_reply_enabled=True, replies_enabled=True, reply_enabled=True
    ).model_dump()
    browsing.store.set("browsing_policy", config)
    event = {
        "event_id": "event-1",
        "kind": "mention",
        "resource_type": "video",
        "account_id": "100",
        "sender_uid": "300",
        "comment_id": "502",
        "timestamp": time.time() - 60,
    }
    browsing.adapter.feed["mention"] = [event]
    await browsing.preflight()
    await browsing.sync_notifications()
    assert browsing.inbox()["items"][0]["status"] == "historical"
    event = dict(event, event_id="event-2", timestamp=time.time() + 0.01)
    browsing.adapter.feed["mention"] = [event]
    browsing.adapter.feed["reply"] = [dict(event, kind="reply")]
    await finished(browsing, preview=True, notifications_only=True)
    assert len(browsing.intents()) == 1
    assert browsing.intents()[0]["target"]["parent"] == "502"
    assert browsing.adapter.sent == []


async def test_cache_pins_drafts_and_clear_does_not_clear_audit(browsing):
    await finished(browsing, preview=True)
    status = browsing.runtime.cache.status()
    assert status["categories"]["browsing"]["protected_bytes"] > 0
    assert status["settings"]["music_limit_mib"] == 15360
    await browsing.runtime.cache.clear("browsing")
    assert browsing.evidence.load(browsing.intents()[0]["material_keys"][0])
    browsing.review(browsing.intents()[0]["id"], "reject")
    browsing.runtime.cache.leases.clear()
    await browsing.runtime.cache.clear("browsing")
    assert len(browsing.intents()) == 1
    with pytest.raises(ValueError, match="清理"):
        browsing.evidence.load(browsing.intents()[0]["material_keys"][0])
    task = browsing.tasks()[0]
    assert task["cache_status"] == "missing" and task["evidence"] == []
    stored = json.loads(browsing.store.one("SELECT payload FROM browsing_tasks")["payload"])
    assert "evidence" not in stored and "description" not in stored.get("metadata", {})


async def test_long_cc_chunks_use_provider_tokens_and_keep_every_reference(browsing):
    await browsing.preflight()
    browsing.settings.data["llm"]["context_tokens"] = 4096
    browsing.store.set("browsing_policy", browsing.policy() | {"decision_max_tokens": 1024})
    segments = [{"id": str(i), "start": i, "end": i + 1, "text": "盐水实验浮力测试" * 70} for i in range(30)]
    material = {
        "metadata": {"parts": [{"cid": "11", "title": "第一P"}]},
        "parts": [{"cid": "11", "segments": segments}],
        "coverage": "all_available_cc",
        "missing_parts": [],
    }
    row, _ = browsing.repo.create_round("100", browsing.runtime.character, browsing.policy(), 0, True)
    chunks = await browsing.decisions._chunks(material, row)
    assert len(chunks) > 1
    assert [s["id"] for chunk in chunks for s in chunk] == [s["id"] for s in segments]
    from tuber_studio.operations.browsing.inputs import ContentDecisionInput

    presentation = ContentDecisionInput(material)
    for chunk in chunks:
        assert (
            await browsing.runtime.llm.count_tokens(
                [{"role": "user", "content": json.dumps(presentation.present(chunk)[0], ensure_ascii=False)}]
            )
            <= 2048
        )


async def test_restart_never_replays_or_restores_publishing(browsing):
    await finished(browsing, preview=True)
    browsing.store.execute("UPDATE browsing_intents SET status='publishing'")
    browsing.repo.recover()
    assert browsing.intents()[0]["status"] == "publish_uncertain"
    assert not browsing.adapter.sent


async def test_notification_private_handoff_owned_by_round_and_never_public_body(browsing, monkeypatch):
    browsing.store.set(
        "browsing_policy",
        BrowsingPolicy(mentions_enabled=True, mention_reply_enabled=True, reply_enabled=True).model_dump(),
    )
    browsing.store.set("message_policy", {"enabled": True, "allow_comment_handoff": True})
    await browsing.preflight()
    await browsing.sync_notifications()
    browsing.adapter.feed["mention"] = [
        {
            "event_id": "private-event",
            "kind": "mention",
            "resource_type": "video",
            "account_id": "100",
            "sender_uid": "300",
            "comment_id": "502",
            "timestamp": time.time(),
        }
    ]
    browsing.runtime.llm.response_action = "handoff_private"
    original_thread = browsing.adapter.read_public_thread

    async def invited_thread(event):
        value = await original_thread(event)
        value["source"]["text"] = "@大肥鱼 请私信告诉我盐水鸡蛋实验怎么准备"
        value["items"] = [
            {"comment_id": "501", "thread_id": "501", "platform_user_id": "100", "display_name": "大肥鱼", "content": "准备聊实验"},
            {"comment_id": "502", "thread_id": "501", "parent_comment_id": "501", "platform_user_id": "300", "display_name": "邻居", "content": value["source"]["text"], "mentions": [{"uid": "100", "name": "大肥鱼"}]},
        ]
        return value

    monkeypatch.setattr(browsing.adapter, "read_public_thread", invited_thread)
    row = await finished(browsing, preview=True, notifications_only=True)
    assert row["status"] == "completed", row
    intent = browsing.intents()[0]
    assert intent["status"] == "private_handoff" and intent["text"] == ""
    draft = browsing.store.one("SELECT * FROM dm_drafts")
    assert draft["status"] == "pending_review" and draft["account_id"] == "100"
    assert json.loads(draft["video_evidence"])["origin_round_id"] == row["id"]
    assert browsing.messages.queue.empty() and not browsing.adapter.sent
    relation = browsing.runtime.relationships.view(
        browsing.runtime.character["id"], "bilibili", "300", account_id="100"
    )
    assert relation["affinity"] == 1
    assert draft["text"] not in json.dumps(browsing.inbox(), ensure_ascii=False)
    browsing.store.execute("UPDATE browsing_rounds SET status='cancelled' WHERE id=?", (row["id"],))
    browsing.store.execute("UPDATE dm_drafts SET status='cancelled' WHERE id=?", (draft["id"],))
    browsing.messages.retry(draft["id"])
    browsing.messages.queue.get_nowait()
    browsing.messages.queue.task_done()
    await browsing.messages._generate(draft["id"], automatic=False)
    retried = browsing.store.one("SELECT * FROM dm_drafts WHERE id=?", (draft["id"],))
    assert retried["status"] == "pending_review"
    assert not json.loads(retried["video_evidence"]).get("origin_round_id")
    assert not browsing.adapter.sent


async def test_worker_crash_has_bounded_takeover_without_losing_other_video(browsing):
    original = browsing.adapter.video_metadata
    attempts = 0

    async def fail_once(video):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("worker lost before platform write")
        return await original(video)

    browsing.adapter.video_metadata = fail_once
    row = await finished(browsing, preview=True)
    assert row["status"] == "completed", row
    assert row["metrics"]["takeovers"] == 1 and attempts == 2
    assert browsing.tasks()[0]["generation"] == 2


async def test_automatic_queue_wait_rechecks_old_source_draft(browsing):
    # A manual workflow wins the GPU lock while a notification is waiting.
    browsing.comments.import_items(
        BV,
        [
            {
                "comment_id": "502",
                "thread_id": "501",
                "platform_user_id": "300",
                "content": "大肥鱼你怎么看盐水里的鸡蛋？",
            }
        ],
    )
    comment = browsing.store.one("SELECT id FROM comments")["id"]
    config = BrowsingPolicy(
        mentions_enabled=True, mention_reply_enabled=True, reply_enabled=True
    ).model_dump()
    browsing.store.set("browsing_policy", config)
    await browsing.preflight()
    await browsing.sync_notifications()
    browsing.adapter.feed["mention"] = [
        {
            "event_id": "duplicate",
            "kind": "mention",
            "resource_type": "video",
            "account_id": "100",
            "sender_uid": "300",
            "comment_id": "502",
            "timestamp": time.time(),
        }
    ]
    await browsing.runtime.generation_lock.acquire()
    response = await browsing.run(preview=True, notifications_only=True)
    try:
        for _ in range(100):
            if browsing.store.one("SELECT id FROM browsing_events WHERE status='context_ready'"):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("notification did not reach waiting generation")
        browsing.store.execute(
            "INSERT INTO drafts(comment_id,text,status,character_id,character_version,account_id) VALUES(?,?,'pending_review',?,?,'100')",
            (comment, "人工候选", browsing.runtime.character["id"], browsing.runtime.character["version"]),
        )
    finally:
        browsing.runtime.generation_lock.release()
    await browsing.active[response["id"]]
    assert not browsing.intents() and not browsing.runtime.llm.calls


async def test_reread_really_refreshes_raw_material_but_keeps_rejection(browsing):
    await finished(browsing, preview=True)
    browsing.review(browsing.intents()[0]["id"], "reject")
    before = len([c for c in browsing.adapter.calls if c[0] == "metadata"])
    response = await browsing.reread(browsing.tasks()[0]["id"])
    await browsing.active[response["id"]]
    assert len([c for c in browsing.adapter.calls if c[0] == "metadata"]) == before + 1
    assert len(browsing.intents()) == 1 and browsing.intents()[0]["status"] == "rejected"


async def test_autonomous_like_and_favorite_intents(browsing):
    browsing.store.set(
        "browsing_policy",
        BrowsingPolicy(
            top_level_enabled=True,
            video_like_enabled=True,
            video_fav_enabled=True,
        ).model_dump(),
    )
    await finished(browsing, preview=True)
    intents = browsing.intents()
    kinds = [intent["kind"] for intent in intents]
    assert "top_level" in kinds
    assert "video_like" in kinds
    assert "video_fav" in kinds

    like_intent = next(i for i in intents if i["kind"] == "video_like")
    assert like_intent["status"] == "pending_review"
    assert "点赞" in like_intent["text"]

    fav_intent = next(i for i in intents if i["kind"] == "video_fav")
    assert fav_intent["status"] == "pending_review"
    assert "收藏" in fav_intent["text"]


async def test_autonomous_like_and_favorite_auto_publish(browsing):
    browsing.store.set(
        "browsing_semantic_validation",
        {"passed": True, "signature": browsing.model_signature()},
    )
    browsing.operations.configure({"enabled": True}, account_id="100", character=browsing.runtime.character)
    browsing.store.set(
        "browsing_policy",
        BrowsingPolicy(
            top_level_enabled=True,
            top_level_auto_publish=True,
            video_like_enabled=True,
            video_like_auto_publish=True,
            video_fav_enabled=True,
            video_fav_auto_publish=True,
        ).model_dump(),
    )
    row = await browsing.run(automatic=True)
    await browsing.active[row["id"]]
    intents = browsing.intents()
    kinds = [intent["kind"] for intent in intents]
    assert "video_like" in kinds
    assert "video_fav" in kinds

    like_intent = next(i for i in intents if i["kind"] == "video_like")
    assert like_intent["status"] == "published"
    assert like_intent["auto_authorized"] == 1

    fav_intent = next(i for i in intents if i["kind"] == "video_fav")
    assert fav_intent["status"] == "published"
    assert fav_intent["auto_authorized"] == 1


async def test_model_selects_batch_order_and_parallel_agent_count(browsing):
    videos = [BV, "BV1xx411c7mD", "BV17x411w7KC"]
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure(
        {
            "sources": ["specified"],
            "watch_videos": videos,
            "workers": 4,
            "inspect_limit": 2,
            "recent_sample_limit": 0,
            "decision_max_tokens": 512,
        }
    )
    original = browsing.runtime.llm.generate_json

    async def choose(messages, schema, *, max_tokens):
        if schema["title"] == "CandidateBatchPlan":
            packet = model_packet(messages[-1]["content"])
            assert [item["bvid"] for item in packet["candidates"]] == videos
            return batch_plan_wire([
                {"bvid": bvid, "action": "watch" if bvid != BV else "skip", "explanation": "按兴趣安排这一条"}
                for bvid in reversed(videos)
            ])
        return await original(messages, schema, max_tokens=max_tokens)

    browsing.runtime.llm.generate_json = choose
    await browsing._current_account()
    row = await finished(browsing, automatic=True, video_only=True)
    plan = row["metrics"]["batch_plan"]
    assert plan["videos"] == list(reversed(videos[1:]))
    assert plan["workers"] == 1
    assert [item["bvid"] for item in plan["choices"]] == list(reversed(videos))
    assert [item["action"] for item in plan["choices"]] == ["watch", "watch", "skip"]
    assert row["metrics"]["workers"] == 1
    assert list(dict.fromkeys(call[1] for call in browsing.adapter.calls if call[0] == "metadata")) == list(
        reversed(videos[1:])
    )
    assert len(browsing.adapter.sent) == 2


async def test_revisited_video_replies_to_new_comments_without_another_main_reply_across_versions(browsing):
    browsing.comments.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing.configure(
        {
            "reply_enabled": True,
            "max_replies_per_video": 0,
            "recent_sample_limit": 0,
            "decision_max_tokens": 512,
            "recipient_cooldown_seconds": 0,
        }
    )
    incoming = "501"

    async def fetch(video, max_pages=1):
        return {
            "items": [
                {
                    "comment_id": incoming,
                    "thread_id": incoming,
                    "platform_user_id": "300",
                    "display_name": "观众",
                    "content": "这个盐水实验要准备什么？",
                }
            ]
        }

    async def read_reply(target):
        return {"author_uid": "300", "text": "这个盐水实验要准备什么？"}

    browsing.adapter.fetch = fetch
    browsing.adapter.read_video_reply = read_reply
    await browsing._current_account()
    await finished(browsing, automatic=True, video_only=True)
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    browsing.store.save_character(browsing.runtime.character)
    for value in browsing.store.all("SELECT path FROM browsing_materials"):
        from pathlib import Path

        Path(value["path"]).unlink(missing_ok=True)
    browsing.store.execute("DELETE FROM browsing_materials")
    incoming = "502"
    browsing.operations.configure(
        {"enabled": True}, account_id="100", character=browsing.runtime.character
    )
    await finished(browsing, automatic=True, video_only=True)
    await finished(browsing, automatic=True, interaction_only=True)
    await wait_for_sends(browsing)
    assert [target["parent"] for target, _ in browsing.adapter.sent] == ["0", "501", "502"], (
        [(item["kind"], item["status"], item["error"]) for item in browsing.intents()],
        browsing.operations.last_send_error,
        browsing.store.all("SELECT status,error FROM browsing_events"),
    )
    assert not [name for name, _ in browsing.runtime.llm.calls if name == "ContentDecision"]
    assert len([name for name, _ in browsing.runtime.llm.calls if name == "ResponseDecision"]) == 3
    assert len([item for item in browsing.intents() if item["kind"] == "top_level"]) == 1
    assert len([call for call in browsing.adapter.calls if call[0] == "subtitles"]) == 1
    assert browsing.repo.content_progress({"account_id": "another", "character_id": "whale"}, BV) is None


async def test_comment_scroll_keeps_latest_arrivals_and_advances_persisted_old_page(browsing):
    await browsing._current_account()
    calls = []

    async def fetch(video, max_pages=1, *, start_page=1, newest=False):
        calls.append((start_page, newest))
        return {
            "items": [{"comment_id": "new" if newest else str(start_page)}],
            "next_page": None if start_page == 3 else start_page + 1,
        }

    browsing.adapter.fetch = fetch
    row = {"preview": False, "account_id": "100", "character_id": "whale"}
    first = await browsing.video_comment_context(row, BV)
    assert browsing.store.get(f"browsing_comment_page:100:whale:{BV}") is None
    browsing.commit_comment_context(first)
    second = await browsing.video_comment_context(row, BV)
    browsing.commit_comment_context(second)
    assert [item["comment_id"] for item in first["items"]] == ["new", "1"]
    assert [item["comment_id"] for item in second["items"]] == ["new", "2"]
    assert calls == [(1, True), (1, False), (1, True), (2, False)]
    assert browsing.store.get(f"browsing_comment_page:100:whale:{BV}") == 3
