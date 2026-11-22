"""Shared fixtures, fake adapters, and helper builders for `tests/operations/` and migrated callers."""

import asyncio
import base64
import copy
import datetime as dt
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio

from tests.support.builders import (
    authorize_automation,
    browsing,
    browsing_finished,
    comments,
    dm_finished,
    draft,
    incoming,
    messages,
    public_memory,
    published_reply,
    service,
    start_automation,
    wait_for_sends,
)
from tests.support.fake_bilibili import (
    BV,
    CommentPlatform,
    ControlledPlatform,
    DMDelivery,
)
from tests.support.fake_providers import (
    CONFIG,
    FRAMES,
    META,
    PNG,
    ControlledModel,
    DynamicModel,
    OfflineChat,
    batch_plan_wire,
    generations,
    model_packet,
    provider,
    providers,
)
from tuber_studio.core.meter import GenerationMeter, reserve_generation, settle_generation
from tuber_studio.core.models import OperationReply
from tuber_studio.core.turns import operation_lane
from tuber_studio.infra.inference.api_providers import APIProvider
from tuber_studio.infra.platforms.bilibili.account import BilibiliAccount
from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.infra.platforms.bilibili.client import PlatformReadError
from tuber_studio.infra.platforms.bilibili.messages import BilibiliMessagesAdapter
from tuber_studio.operations.channels.comments import Comments
from tuber_studio.operations.channels.comments.timeline import observe_thread
from tuber_studio.operations.channels.direct_messages import DirectMessages
from tuber_studio.operations.discovery.repository import DiscoveryRepository
from tuber_studio.operations.kernel.evidence import BrowsingEvidence
from tuber_studio.operations.kernel.models import BrowsingPolicy
from tuber_studio.operations.service import BrowsingService
from tuber_studio.persona.assets.memes import MemeCatalog

finished = browsing_finished
finished_message = dm_finished
Platform = CommentPlatform
Delivery = DMDelivery
Model = DynamicModel
ACTOR = "injected-test-account"


async def authorize(browsing_service, identity="durable-batch"):
    browsing_service.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await authorize_automation(browsing_service)


def candidate(number):
    return {"bvid": f"BV{number:010d}", "title": f"独立视频{number}", "duration": 30}


class PublicAdapter:
    def __init__(self):
        self.calls = []
        self.metadata = {
            "bvid": BV,
            "aid": 123,
            "title": "海底观察",
            "description": "海底生物公开资料",
            "tags": ["科普"],
            "owner_uid": "77",
            "owner": "作者",
            "duration": 120,
            "published_at": 200,
            "category_id": 1,
            "parts": [
                {"cid": "456", "title": "第一P", "duration": 60},
                {"cid": "457", "title": "第二P", "duration": 60},
            ],
        }
        self.failures = {}
        self.subtitle_rows = {}
        self.metadata_gate = None
        self.subtitle_gate = None
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.frames = [
            {
                "id": "first",
                "cid": "456",
                "requested_time": 10,
                "actual_time": 8,
                "width": 1,
                "height": 1,
                "kind": "storyboard",
                "image": PNG,
            }
        ]

    async def video_metadata(self, video):
        self.calls.append(("metadata", video))
        if self.metadata_gate is not None:
            await self.metadata_gate.wait()
        return copy.deepcopy(self.metadata) | {"fetched_at": time.time()}

    async def subtitles(self, video, cid, languages):
        self.calls.append(("subtitles", video, cid, list(languages)))
        self.entered.set()
        try:
            if self.subtitle_gate is not None:
                await self.subtitle_gate.wait()
            if cid in self.failures:
                raise PlatformReadError(self.failures[cid], "分类后的平台失败")
            return copy.deepcopy(
                self.subtitle_rows.get(
                    cid,
                    {
                        "bvid": BV,
                        "cid": cid,
                        "track_id": "1",
                        "language": "zh",
                        "is_ai": cid == "457",
                        "segments": [{"id": f"{cid}:1:0", "start": 1, "end": 2, "text": "章鱼有三个心脏"}],
                        "coverage": {"complete": True, "start": 1, "end": 2, "segment_count": 1},
                        "fetched_at": time.time(),
                    },
                )
            )
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def video_frames(self, metadata, count, *, cid=None, timestamps=None, windows=None):
        self.calls.append(("frames", count, cid, timestamps, windows))
        frames = [frame for frame in self.frames if cid is None or frame["cid"] == cid or frame["cid"] == "999"]
        if windows:
            frames = [
                frame
                for frame in frames
                if not isinstance(frame["actual_time"], (int, float))
                or not 0 <= frame["actual_time"] <= next(part["duration"] for part in metadata["parts"] if part["cid"] == cid)
                or any(start <= frame["actual_time"] <= end for start, end in windows)
            ]
        return copy.deepcopy(frames[:count])


class TestVisual:
    __test__ = False

    def __init__(self):
        self.calls = 0
        self.cancelled = []
        self.started = asyncio.Event()
        self.gate = None
        self.task = None
        self.frames = None
        self.returned = {}
        self.closed = False
        self.cancel_error = None
        self.cap = {
            "provider_id": "controlled-test",
            "version": "1",
            "available": True,
            "images": True,
            "multiple_images": True,
            "admitted": True,
            "max_frames": 6,
            "max_bytes": 4096,
            "max_pixels": 100,
            "context_tokens": 4096,
        }

    def capabilities(self):
        return self.cap

    async def analyze_video_evidence(self, task, metadata, frames, question, *, deadline):
        self.calls += 1
        self.task, self.frames = task, frames
        self.deadline = deadline
        self.received = copy.deepcopy(frames)
        self.started.set()
        if self.gate:
            await self.gate.wait()
        return {
            "observations": "所列画面里可见珊瑚",
            "evidence_ids": [frame["id"] for frame in frames],
            "frame_observations": {frame["id"]: "所列画面里可见珊瑚" for frame in frames},
            "provider_id": "controlled-test",
            "version": "1",
            "limitations": ["仅抽样帧，不推断动作与声音"],
        } | self.returned

    async def cancel(self, task_id):
        self.cancelled.append(task_id)
        if self.cancel_error:
            raise self.cancel_error

    async def close(self):
        self.closed = True


async def evidence_reader(method, *args):
    return await method(*args)


@pytest.fixture
def config():
    return BrowsingPolicy().model_dump()


@pytest_asyncio.fixture
async def evidence_factory(runtime):
    instances = []

    def create(adapter=None, visual=None):
        ev = BrowsingEvidence(runtime, adapter or PublicAdapter(), evidence_reader, visual)
        instances.append(ev)
        return ev

    yield create
    for instance in instances:
        await instance.close()


class FollowupVisual:
    def __init__(self, runtime):
        self.runtime = runtime
        self.started, self.gate = asyncio.Event(), None
        self.generated, self.cancelled = 0, []

    def capabilities(self):
        return {
            "provider_id": "followup-test",
            "version": "1",
            "available": True,
            "images": True,
            "admitted": True,
            "multiple_images": True,
            "max_frames": 3,
            "max_pixels": 100,
            "max_bytes": 4096,
            "context_tokens": 4096,
        }

    async def analyze_video_evidence(self, task, metadata, frames, question, *, deadline):
        self.task, self.deadline = task, deadline
        async with self.runtime.offline_generation():
            with GenerationMeter(self.runtime.browsing_repository, task["round_id"]):
                reservation = reserve_generation(8, 16)
                self.generated += 1
                self.started.set()
                if self.gate:
                    await self.gate.wait()
                settle_generation(reservation, {"prompt_tokens": 8, "completion_tokens": 5})
        return {
            "sufficient": True,
            "observations": "蓝色液体位于透明杯中。",
            "evidence_ids": [frame["id"] for frame in frames],
            "frame_observations": {frame["id"]: "蓝色液体位于透明杯中。" for frame in frames},
            "limitations": [],
            "provider_id": "followup-test",
            "version": "1",
        }

    async def cancel(self, task_id):
        self.cancelled.append(task_id)

    async def close(self):
        pass


def install_visual(service_obj, *, ask=True, ask_again=False, initial_time=8):
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jHtkAAAAASUVORK5CYII="
    )
    visual = FollowupVisual(service_obj.runtime)
    visual.prompts = []
    service_obj.evidence.visual = visual

    async def frames(metadata, count, *, cid=None, timestamps=None):
        service_obj.adapter.calls.append(("frames", count))
        actual = initial_time if not timestamps else timestamps[0]
        return [
            {
                "id": f"{cid or '11'}:{actual:g}",
                "cid": cid or "11",
                "actual_time": actual,
                "requested_time": actual,
                "width": 1,
                "height": 1,
                "image": image,
                "kind": "storyboard",
            }
        ]

    service_obj.adapter.video_frames = frames
    original = service_obj.runtime.operation_llm.generate_json
    inputs = []

    async def generate(messages_in, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            result = await original(messages_in, schema, max_tokens=max_tokens)
            latest_frame = re.findall(r"(?m)^\[(ref:\d+)\|[^\]]+\]P\d+ 画面", messages_in[-1]["content"])
            if latest_frame:
                result["evidence_ids"] = latest_frame[-1:]
            return result
        if schema["title"] != "ContentDecision":
            return await original(messages_in, schema, max_tokens=max_tokens)
        data = model_packet(messages_in[-1]["content"])
        visual.prompts.append(
            "\n".join(message["content"] for message in messages_in if message["role"] == "system")
        )
        inputs.append(data)
        for branch in schema["oneOf"]:
            fields = branch["properties"]
            if len(inputs) > 1:
                assert "visual" not in fields["coverage_required"]["enum"]
                assert "visual_required" not in fields["reason"]["enum"]
        visual_text = data["content"].partition("画面观察\n")[2]
        visual_ids = re.findall(r"(?m)^\[(e\d+)\|", visual_text)
        if ask and (len(inputs) == 1 or ask_again):
            return {
                "interest": "need_more",
                "worth_commenting": False,
                "reason": "visual_required",
                "coverage_required": "visual",
                "evidence_ids": [data["segments"][0]["id"]],
            }
        if visual_ids:
            return {
                "interest": "like",
                "worth_commenting": True,
                "reason": "specific_detail",
                "coverage_required": "provided",
                "evidence_ids": visual_ids,
            }
        return await original(messages_in, schema, max_tokens=max_tokens)

    service_obj.runtime.operation_llm.generate_json = generate
    return visual, inputs


@pytest.fixture
async def image_http(settings):
    content = (settings.root / "assets/whale/cheerful.gif").read_bytes()
    state = {
        "requests": [],
        "upload": "ok",
        "final": "ok",
        "after_upload": None,
        "content": content,
        "receipt_patch": {},
        "upload_started": asyncio.Event(),
        "nav_entered": asyncio.Event(),
        "nav_wait": None,
    }

    async def handle(request):
        state["requests"].append(request)
        if request.url.path == "/x/web-interface/nav":
            state["nav_entered"].set()
            if state["nav_wait"]:
                await state["nav_wait"].wait()
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "mid": 900,
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                        },
                    },
                },
            )
        if request.url.path == "/x/dynamic/feed/draw/upload_bfs":
            state["upload_started"].set()
            if state["upload"] == "block":
                await asyncio.Event().wait()
            if state["upload"] == "timeout":
                raise httpx.ReadTimeout("controlled preparation timeout")
            if state["upload"] == "failed":
                return httpx.Response(503, json={"code": -1})
            if state["after_upload"]:
                state["after_upload"]()
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "image_url": "https://i0.hdslb.com/bfs/new_dyn/known.gif",
                        "image_width": 384,
                        "image_height": 384,
                        "img_size": len(content) / 1024,
                    }
                    | state["receipt_patch"],
                },
            )
        if request.url.path == "/svr_sync/v1/svr_sync/fetch_session_msgs":
            assert request.method == "GET"
            return httpx.Response(200, json={"code": 0, "data": {"messages": []}})
        assert request.url.host == "api.vc.bilibili.com" and request.url.path == "/web_im/v1/web_im/send_msg"
        if callback := state.get("on_message"):
            return callback(parse_qs(request.content.decode()))
        if state["final"] == "timeout":
            raise httpx.ReadTimeout("controlled unknown final write")
        if state["final"] == "no_receipt":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {"msg_key": "456"}})

    adapter = BilibiliMessagesAdapter({"sessdata": "fixture-session", "csrf": "fixture-csrf"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handle)
    )
    yield adapter, state
    await adapter.close()


@pytest.fixture
async def meme_messages(runtime, settings, store, image_http):
    catalog = MemeCatalog(settings, store)
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    adapter, state = image_http
    asset = await catalog.register(
        "whale",
        state["content"],
        title="角色开心表情",
        emotions=["cheerful"],
        source={"kind": "user_upload", "author": "test operator"},
        license={"name": "fixture only"},
    )
    cmt = Comments(settings, store, runtime, BilibiliAccount(settings, store))
    msg = DirectMessages(settings, store, runtime, cmt, adapter=adapter, memes=catalog)
    cmt.direct_messages = msg
    await msg.configure({"enabled": True})
    await msg.open()
    yield msg, catalog, asset, state
    await msg.close()
    await cmt.close()
    await cmt.account.close()


async def dm_finished(svc, identity):
    async with asyncio.timeout(3):
        while True:
            row = svc.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if row["status"] not in {"queued", "generating", "publishing"}:
                return row
            await asyncio.sleep(0.01)


async def prepared(bundle, *, image=True, automatic=False, account_id=""):
    msg, _, asset, _ = bundle
    source, _ = msg.ingest(
        {
            "message_id": "received-current-user",
            "platform_user_id": "123",
            "display_name": "鱼宝",
            "text": "你好呀",
            "timestamp": time.time(),
            "account_id": account_id,
        }
    )
    identity = msg.enqueue("message", source, automatic=automatic)
    async with asyncio.timeout(4):
        while True:
            row = msg.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if row["status"] not in {"queued", "generating", "publishing"}:
                break
            await asyncio.sleep(0.005)
    if image:
        msg.review(identity, "edit", meme_id=asset["asset_id"])
        msg.review(identity, "approve")
    return identity


def invitation_source(msg, *, cid="701", root="700", mentioned=ACTOR, trigger="invitation"):
    text = "请把我问的那项配置私信告诉我" if trigger != "agreement" else "查到了私信告诉我"
    msg.comments.import_items(
        "BV1d8pU6xEZX",
        [
            {
                "comment_id": cid,
                "thread_id": root,
                "parent_comment_id": root,
                "platform_user_id": "123",
                "display_name": "鱼宝",
                "content": text,
            }
        ],
    )
    source = msg.store.one("SELECT * FROM comments WHERE comment_id=?", (cid,))
    thread = {
        "source": {"comment_id": cid, "author_uid": "123", "author_name": "鱼宝", "text": text},
        "target": {"type": 1, "oid": "42", "bvid": source["video_id"], "root": root, "parent": cid},
        "root": {"comment_id": root, "author_uid": ACTOR, "text": "我方此前的公开讨论"},
        "parent": {"comment_id": root, "author_uid": ACTOR, "text": "我方此前的公开讨论"},
        "addressing": {"source_mentions": [{"uid": mentioned, "name": "同名大肥鱼"}], "source_parent_comment_id": root},
    }
    return source, thread


def invite(msg, *, cid="701", root="700", mentioned=ACTOR, trigger="invitation", proposal=True):
    source, thread = invitation_source(msg, cid=cid, root=root, mentioned=mentioned, trigger=trigger)
    identity = msg.enqueue(
        "comment",
        source["id"],
        automatic=True,
        account_id=ACTOR,
        purpose="讨论你问的配置位置",
        contact_trigger=trigger,
        schedule=False,
        proposed_text="配置在设置页，我来接着说。" if proposal else "",
        proposal_identity=msg.runtime.identity_snapshot(msg.runtime.character, ACTOR),
        video_evidence={"material_keys": [], "source_versions": [], "public_source_thread": thread},
    )
    return identity


async def model_for(
    runtime,
    settings,
    *,
    first=None,
    corrected=None,
    first_finish="stop",
    corrected_finish="stop",
    decision="revise",
    failure=None,
):
    requests, generated = [], []
    if corrected is None:
        corrected = {"spoken_text": "那本鱼先把软垫给你挪一点，你慢慢说。", "reply_to": ["source-event"]}

    async def respond(request):
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        probe, finish = body["max_tokens"] == 1, "stop"
        if probe:
            text = ""
        elif "read" in body["response_format"].get("json_schema", {}).get("schema", {}).get("properties", {}):
            text = json.dumps({"action": decision, "reason": "继续当前这次交流", "read": ""}, ensure_ascii=False)
        else:
            if failure is not None and (error := failure(body)):
                raise error
            value = (first if first is not None else corrected) if not generated else corrected
            finish = first_finish if not generated else corrected_finish
            generated.append(value)
            text = json.dumps(value, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 1 if probe else 20},
            },
        )

    cfg = settings.data["omni"] | {
        "base_url": "https://isolated.invalid/v1",
        "model": "test-model",
        "context_tokens": 131072,
        "native_context_tokens": 131072,
        "enable_thinking": True,
    }
    prov = APIProvider(cfg | {"structured_output": "json_schema", "context_source": "declared", "prompt_token_endpoint": False})
    await prov.client.aclose()
    prov.client = httpx.AsyncClient(
        base_url="https://isolated.invalid/v1", transport=httpx.MockTransport(respond)
    )
    await runtime.operation_llm.close()
    runtime.operation_llm = prov
    return requests, generated, corrected


def original_and_draft(cmt, *, status="published", receipt=None, suffix="1"):
    st, char = cmt.store, cmt.runtime.character
    cmt.import_items(
        "import-delivery",
        [{"comment_id": suffix, "thread_id": "root", "platform_user_id": "neighbor", "content": f"保留的原观众问题{suffix}"}],
    )
    source = st.one("SELECT * FROM comments WHERE comment_id=?", (suffix,))
    text = f"我去了银月书店{suffix}，店主小壳送我三本诗集。"
    identity = st.execute(
        "INSERT INTO drafts(comment_id,text,status,character_id,character_version,account_id,receipt) VALUES(?,?,?,?,?,'100',?)",
        (source["id"], text, status, char["id"], char["version"], json.dumps(receipt) if receipt else None),
    ).lastrowid
    scope = cmt.account_context_scope(source["video_id"], source["thread_id"], account_id="100")
    event = cmt._comment_event(source) | {"account_id": "100"}
    cmt.runtime.operation_context.record(scope, char, event, text, "comment_draft")
    return scope, source, event, text, identity


async def prepared_row(browsing_service):
    await browsing_service.preflight(for_automation=False)
    row, _ = browsing_service.repo.create_round("100", browsing_service.runtime.character, browsing_service.policy(), 0, True)
    return row


# --- Extracted from comment_only & activity_history ---

async def enable_comment_choices(browsing_service, monkeypatch, *, automatic=False):
    await browsing_service.configure({
        "sources": ["specified"], "watch_videos": [BV], "discovery_enabled": True,
        "top_level_enabled": True, "top_level_auto_publish": True,
        "reply_enabled": True, "reply_auto_publish": automatic,
        "recent_sample_limit": 0, "max_replies_per_video": 0, "recipient_cooldown_seconds": 0,
    })
    browsing_service.bilibili_account.credentials = {"sessdata": "test", "csrf": "test"}
    await browsing_service.set_enabled(True)
    original = browsing_service.runtime.operation_llm.generate_json
    mode = {"action": "comments"}
    calls = []

    async def choose(prompt_messages, schema, *, max_tokens):
        calls.append((schema["title"], operation_lane.get()))
        if schema["title"] == "CandidateBatchPlan":
            candidates = model_packet(prompt_messages[-1]["content"])["candidates"]
            return batch_plan_wire([
                {"bvid": item["bvid"], "action": mode["action"], "explanation": "先看看评论里大家用了多少盐，再接着聊"}
                for item in candidates
            ])
        return await original(prompt_messages, schema, max_tokens=max_tokens)

    monkeypatch.setattr(browsing_service.runtime.operation_llm, "generate_json", choose)
    return mode, calls


def comment(identity="601", root="601"):
    return {"comment_id": identity, "thread_id": root, "platform_user_id": "300",
            "display_name": "邻居", "content": "盐水浮鸡蛋要准备多少盐？"}


def attach_comment_source(browsing_service, monkeypatch):
    async def fetch(video, max_pages=1):
        return {"items": [comment()]}

    async def read_reply(target):
        return {"author_uid": "300", "text": comment()["content"]}

    monkeypatch.setattr(browsing_service.adapter, "fetch", fetch)
    monkeypatch.setattr(browsing_service.adapter, "read_video_reply", read_reply, raising=False)


def forbid_video_content(browsing_service, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("A comment-only task must not read captions/frames or start video processing")

    monkeypatch.setattr(browsing_service, "_prepare_evidence", forbidden)
    monkeypatch.setattr(browsing_service.adapter, "subtitles", forbidden)


async def finish_comment_round(browsing_service):
    row = await finished(browsing_service, automatic=True, video_only=True)
    assert row["status"] == "completed"
    await wait_for_sends(browsing_service)
    return row


def stamp(value):
    return dt.datetime.fromisoformat(value).timestamp()


@pytest.fixture
def activity_service(store):
    repo = DiscoveryRepository(store)
    return SimpleNamespace(
        store=store,
        repo=repo,
        policy=lambda: {"business_timezone": "Asia/Taipei"},
        discovery=SimpleNamespace(history_scope=lambda: ("100", "whale", 99)),
    )


def round_row(svc, *, account="100", version=1, batch=False):
    config = BrowsingPolicy().model_dump() | {"discovery_publish": True}
    if batch:
        config["autonomous_batch"] = True
    row, _ = svc.repo.create_round(account, {"id": "whale", "version": version}, config, 0, False)
    svc.store.execute("UPDATE browsing_rounds SET status='completed' WHERE id=?", (row["id"],))
    return svc.repo.round(row["id"])


def task(
    svc,
    row,
    identity,
    at,
    *,
    bvid=BV,
    kind="new_video",
    status="selected",
    explanation="杯里的鸡蛋真的浮起来了，想问问盐水比例。",
    error=None,
):
    payload = {"bvid": bvid, "title": "盐水实验", "task_kind": kind, "explanation": explanation}
    identity = svc.repo.task(row["id"], payload)
    svc.store.execute(
        "UPDATE browsing_tasks SET status=?,material_key=?,payload=?,error=?,created_at=?,updated_at=? WHERE id=?",
        (
            status,
            "kept-material" if status == "selected" else None,
            json.dumps(payload),
            error,
            at,
            at,
            identity,
        ),
    )
    return identity


def reply(svc, row, identity, at, *, bvid=BV, kind="reply_public", status="published"):
    target = {"bvid": bvid, "recipient_name": "实验观众" if kind == "reply_public" else ""}
    svc.store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,status,text,target,receipt,reason,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            row["account_id"],
            "whale",
            row["character_version"],
            identity,
            kind,
            row["id"],
            status,
            "鱼鱼也想试试这个比例。",
            json.dumps(target),
            json.dumps({"rpid": identity, "published_at": at}),
            "specific_detail",
            at,
            at,
        ),
    )


__all__ = [
    "ACTOR",
    "BV",
    "CONFIG",
    "FRAMES",
    "META",
    "PNG",
    "BrowsingAdapter",
    "BrowsingService",
    "CommentPlatform",
    "ControlledModel",
    "ControlledPlatform",
    "DMDelivery",
    "Delivery",
    "DynamicModel",
    "Model",
    "OfflineChat",
    "OperationReply",
    "Platform",
    "activity_service",
    "attach_comment_source",
    "authorize",
    "authorize_automation",
    "batch_plan_wire",
    "browsing",
    "browsing_finished",
    "candidate",
    "comment",
    "comments",
    "config",
    "dm_finished",
    "draft",
    "enable_comment_choices",
    "evidence_factory",
    "evidence_reader",
    "finish_comment_round",
    "finished",
    "finished_message",
    "forbid_video_content",
    "generations",
    "incoming",
    "install_visual",
    "invitation_source",
    "invite",
    "meme_messages",
    "messages",
    "model_for",
    "model_packet",
    "observe_thread",
    "original_and_draft",
    "prepared",
    "prepared_row",
    "provider",
    "providers",
    "public_memory",
    "published_reply",
    "reply",
    "round_row",
    "service",
    "stamp",
    "start_automation",
    "task",
    "wait_for_sends",
]
