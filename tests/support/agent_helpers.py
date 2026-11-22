"""Shared test fixtures and helpers for agent tests."""

import asyncio
import json
import shutil
import subprocess
from uuid import uuid4

import httpx
import pytest

from tuber_studio.agent.chat.lookup import operator_facts
from tuber_studio.agent.tasks.archived_video import ArchivedVideo
from tuber_studio.agent.tasks.delivery import WINDOW_REVIEW_VERSION, PrivateDelivery
from tuber_studio.agent.tasks.identity import TaskIdentity
from tuber_studio.agent.tasks.runtime import TaskRuntime
from tuber_studio.app import create_app
from tuber_studio.core.store import dump
from tuber_studio.infra.inference import http_api as http_api
from tuber_studio.operations.channels.comments.timeline import ensure_comment_timeline

CHAT = "/api/v1/agent/chat"
MEM = "/api/v1/agent/memory"
NOTE = "写开场时我喜欢先讲海底厨房。"

ACCOUNT = "12345"
SOURCE = "BV1ab411c7mD"
OTHER = "BV1ab411c7mE"
PENDING = "BV1ab411c7mF"

BV = "BV1234567890"
ARCHIVE_BV = "BV1aa1234567"
ARCHIVE_URL = f"https://www.bilibili.com/video/{ARCHIVE_BV}/?p=2"


async def until(condition):
    async with asyncio.timeout(8):
        while not (result := condition()):
            await asyncio.sleep(0.02)
    return result


async def wait_status(runtime, identity, status):
    async with asyncio.timeout(5):
        while True:
            task = runtime.current(identity)
            if task["status"] == status:
                return task
            await asyncio.sleep(0.01)


def studio_tasks(store):
    def binding():
        character = store.character()
        account = store.get("browsing_discovery_account", {}).get("account_id", "")
        return [account, character["id"], character["version"], character.get("_edit_identity")]

    return TaskRuntime(store, TaskIdentity(store, lambda: store.get("active_character"), binding), None)


def call(name, args, identity):
    return {"type": "function_call", "name": name, "call_id": identity, "arguments": json.dumps(args)}


def gateway(settings, monkeypatch, reply):
    settings.data["omni"].update(
        base_url="https://example.org/v1",
        responses_model="text-worker",
        responses_options={"max_output_tokens": 1300},
    )
    settings.provider_keys = {"omni": "mock-responses-key"}
    requests = []

    def handle(request):
        assert request.method == "POST" and request.url.path == "/llm/v1/responses"
        assert request.headers["authorization"] == "Bearer mock-responses-key"
        payload = json.loads(request.content)
        assert payload["model"] == "text-worker" and payload["max_output_tokens"] == 1300
        requests.append(payload)
        output = reply(payload)
        if isinstance(output, httpx.Response):
            return output
        return httpx.Response(
            200, json={"id": f"resp-{len(requests)}", "status": "completed", "output": output}
        )

    monkeypatch.setattr(http_api, "inference_transport", lambda base_url: httpx.MockTransport(handle))
    return requests


@pytest.fixture
async def studio(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {settings.admin_token}"},
        ) as client,
    ):
        yield app.state.runtime, client


async def turn(client, message, *, stream=False, **fields):
    request = {"request_id": str(uuid4()), "message": message, **fields}
    response = (await client.post(CHAT + ("/stream" if stream else ""), json=request)).raise_for_status()
    if not stream:
        return response.json()
    return next(
        json.loads(part.split("\ndata: ")[1])
        for part in response.text.split("\n\n")
        if part.startswith("event: done")
    )


def seed_workshop(runtime):
    browsing = runtime.browsing
    char = runtime.character
    now = operator_facts.now(browsing).timestamp()
    browsing.discovery.remember_account(ACCOUNT)
    store = runtime.store
    for account, bvid, citation in (
        (ACCOUNT, SOURCE, "今天的鲸鱼日记提到蓝色灯塔和小瓶子"),
        (ACCOUNT, OTHER, "台风天，记录老码头的浪花"),
        ("888", "BV1ab411c7mG", "另一账号的鲸鱼日记"),
    ):
        body = {
            "metadata": {
                "bvid": bvid,
                "title": "海边的日记",
                "owner": bvid[-1],
                "parts": [{"cid": 100}, {"cid": 101}],
            },
            "parts": [
                {
                    "cid": 101,
                    "visual": True,
                    "segments": [
                        {"start": 19, "end": 22, "text": citation},
                    ],
                }
            ],
        }
        store.execute(
            "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state)"
            " VALUES (?,?,?,?,'[]',?,'viewed')",
            (account, char["id"], bvid, dump(body), now),
        )
    round_id = str(uuid4())
    store.execute(
        "INSERT INTO browsing_rounds(id,account_id,character_id,character_version,status,preview,config)"
        " VALUES (?,?,?,?,'completed',0,'{}')",
        (round_id, ACCOUNT, char["id"], char["version"]),
    )
    for index, (bvid, status, material) in enumerate(
        [
            (SOURCE, "viewed_without_comment", "record-1"),
            (SOURCE, "selected", "record-2"),
            (OTHER, "selected", "record-3"),
            (PENDING, "running", None),
        ]
    ):
        task_id = str(uuid4())
        store.execute(
            "INSERT INTO browsing_tasks(id,round_id,bvid,status,material_key,payload,updated_at) "
            "VALUES (?,?,?,?,?,'{}',?)",
            (task_id, round_id, bvid, status, material, now),
        )
        browsing.repo.quotas.start_watch(task_id, ACCOUNT, char["id"], bvid, 0, now=now + index * 0.001)
    ensure_comment_timeline(store)
    store.execute(
        "INSERT INTO browsing_comment_timeline VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ACCOUNT, char["id"], SOURCE, "12", "12", None, "51", "小灯", "录到了鲸鱼叫声", now, "52"),
    )
    store.execute(
        "INSERT INTO browsing_comment_observations VALUES(?,?,?,?,?,?,?,?)",
        (ACCOUNT, char["id"], SOURCE, "12", "12", "test", round_id, now),
    )


async def save(client, source="settings", **fields):
    body = {"source": source, "text": NOTE, **fields}
    proposal = (await client.post(MEM + "/propose", json=body)).raise_for_status().json()
    response = await client.post(MEM + "/confirm", json=body | {"stamp": proposal["stamp"]})
    return response.raise_for_status().json()["id"]


def frame(seconds, *, part=2, cid="part-two", bvid=BV):
    return {
        "part": part,
        "cid": cid,
        "seconds": seconds,
        "kind": "画面",
        "text": "左侧有一把椅子",
        "url": f"https://www.bilibili.com/video/{bvid}/?p={part}&t={int(seconds)}",
    }


def reading(*frames):
    return {
        "tool": "inspect_video",
        "id": "v",
        "revision": 1,
        "arguments": {"bvid": BV, "part": 2},
        "result": {
            "status": "partial",
            "findings": list(frames),
            "selection": {"bvid": BV, "ranges": [{"part": 2, "start": 0, "end": 300}]},
        },
    }


def page(key):
    return {
        "id": key,
        "tool": "web_page",
        "revision": 1,
        "arguments": {},
        "result": {"url": f"https://example.org/{key}", "excerpt": "剧目资料"},
    }


def selection(*sources, primary=None):
    return PrivateDelivery.model_validate(
        {
            "items": [
                {
                    "title": "开场",
                    "rationale": "我会用这个构图引出话题。",
                    "sources": sources,
                    "primary_frame": primary,
                }
            ],
            "gaps": [],
        }
    )


def short_read(key, start, *, clip=True, revision=1):
    read = reading(*(frame(start + delta) for delta in (0.44, 1.48, 2.52)))
    bounds = {"part": 2, "start": start, "end": start + 3}
    read.update(id=key, revision=revision, arguments={"bvid": BV, **bounds})
    read["result"]["selection"]["ranges"] = [bounds]
    if clip:
        read["result"]["clip"] = {
            "bvid": BV,
            **bounds,
            "cid": "part-two",
            "frames": 75,
            "audio": "visual_only",
        }
    return read


@pytest.fixture
def reviewed_window(monkeypatch):
    def media(_directory, _task, evidence, key):
        return None, evidence.video_coverage[key.rsplit(":", 1)[0]]["clip"]

    async def reviewed(media, *_):
        clip = media[1]
        samples = [
            {"frame": 0, "seconds": clip["start"]},
            {"frame": clip["frames"] - 1, "seconds": clip["end"] - 0.04},
        ]
        return {
            "clip": clip,
            "coverage": {
                "complete": True,
                "uncovered": [],
                "scanned_frames": clip["frames"],
                "samples": samples,
                "start": clip["start"],
                "end": clip["end"],
            },
            "observations": [
                s | {"scene": "符合要求", "captions": "无烧录字幕", "screen_text": ""} for s in samples
            ],
            "caption_topics": [],
            "usage": "仅无剧透素材",
            "accepted": True,
            "screening_version": WINDOW_REVIEW_VERSION,
        }

    monkeypatch.setattr("tuber_studio.agent.tasks.tools.video_grounding.saved_review_media", media)
    monkeypatch.setattr("tuber_studio.agent.tasks.tools.video_grounding.screen_clip_window", reviewed)


@pytest.fixture
def package(tmp_path):
    if not all(shutil.which(tool) for tool in ("ffmpeg", "ffprobe")):
        pytest.skip("需要本地 FFmpeg 与 FFprobe 解码归档原片")
    file = tmp_path / "example.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x48:r=25",
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-bf",
            "0",
            str(file),
        ],
        check=True,
    )
    source = {
        "bvid": ARCHIVE_BV,
        "part": 2,
        "cid": "27",
        "part_title": "第二段",
        "part_duration_seconds": 80,
        "upload_title": "《示例作品》合集",
        "uploader": "作者",
        "url": ARCHIVE_URL,
    }
    source_page = {
        "url": ARCHIVE_URL,
        "title": source["upload_title"],
        "archived_public_metadata": "投稿《《示例作品》合集》，UP 主：作者。合集 P2《第二段》，CID 27，时长 80 秒。",
    }
    programme = {
        "url": "https://tv.cctv.com/2020/01/01/VIDA100.shtml",
        "title": "示例作品_节目",
        "archived_programme_credits": "节目名：示例作品；导演：某某",
    }
    clip = {
        "file": file.name,
        "source_start_seconds": 20,
        "source_end_seconds_exclusive": 22,
        "frames": 50,
        "fps": 25,
        "file_first_video_pts_seconds": 0,
        "file_last_video_pts_seconds": 1.96,
        "source_url": ARCHIVE_URL + "&t=20",
    }
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"source": source, "archived_source_pages": [source_page, programme], "clips": [clip]}),
        encoding="utf-8",
    )
    return ArchivedVideo(path)


def with_frame_structure(model):
    async def generate(messages, schema, **kwargs):
        props = schema["properties"]
        if set(props) == {"observation"}:
            (image,) = messages[-1]["content"]
            assert isinstance(image, dict)
            return {"observation": "独立读图的表面观察"}
        if "claimed_property" in props:
            return {"x": 500, "y": 400, "claimed_property": "材质"}
        if "seen" in props:
            query, image = messages[-1]["content"]
            assert json.loads(query["text"]) == {"横坐标": 500, "纵坐标": 400}
            assert isinstance(image, dict)
            return {"seen": "带纹表面", "not_resolved": "断面未入镜"}
        reply = await model(messages, schema, **kwargs)
        if "alternatives" in props:
            paths = props["alternatives"]["required"]
            return {"alternatives": {path: reply["alternatives"][path] for path in paths}}
        return reply

    return generate
