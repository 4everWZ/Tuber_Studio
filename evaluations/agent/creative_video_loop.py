"""Run with uv run --locked python -m evaluations.agent.creative_video_loop.

Standalone synthetic P2 rehearsal. Uses the same explicit Studio credentials as
evaluations.agent.omni_chat_loop. Only the configured inference gateway is contacted.
The BV and player links are fixture identifiers, not a real Bilibili upload.
"""

import asyncio
import base64
import io
import json
import os
import re
import tempfile
from contextlib import AsyncExitStack, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw

from evaluations.agent.omni_chat_loop import ROOT, ReplayMismatch, connection
from tuber_studio.agent.chat import ChatTurn, OperatorChat
from tuber_studio.agent.tasks.delivery import evidence_index
from tuber_studio.agent.tasks.tools import create_task_runtime
from tuber_studio.core.config import operation_profile
from tuber_studio.core.store import Store
from tuber_studio.infra.inference.api_providers import APIProvider
from tuber_studio.infra.inference.responses_provider import ResponsesClient

BV = "BV1P2Demo001"
ORIGIN = "https://archive.example.invalid"
SOURCE = ORIGIN + "/notes/shape-theatre"
TITLE = "形状剧场 · 作者放映说明"
NOTE = "自造公开样片《形状剧场》。P1 是测试片；P2 前两个镜头是可用于暖场的预告，第三个镜头揭晓结局，请勿剧透。"
BOUNDARY = "本回放使用自造无声动图、实际解码帧和固定出处文字。真实平台取片、音频、字幕及送达分别验收。"
REQUEST = (f"替我准备下场直播的私有提纲。看 {BV} 的 P2，再查作者放映说明 {SOURCE} 。"
           "挑两个不同的不剧透镜头。每段用看到的图形命名，说说为什么适合，分别引用实际画面时码和放映说明，不要发布。")
CHANGE = ("刚才那份提纲改一下，改做三角形主题的开场。仍只选 P2 中出现三角形、又不剧透的不同镜头，"
          "最多两个；不足两个就说明缺口，不要用同一镜头凑数。每段仍引用画面和原出处。")

# Each tuple is one original scene: (color, shape, duration in milliseconds).
PARTS = {1: (("blue", "circle", 2000),), 2: (("yellow", "triangle", 4000),
                                             ("blue", "square", 5000), ("red", "circle", 3000))}
EXPECTED = {1: {0, 4}, 2: {0}}
NAMES = {0: ("三角", "triangle"), 4: ("方形", "方块", "方框", "square")}


def encode_clip(scenes):
    images = []
    for color, shape, _ in scenes:
        frame = Image.new("RGB", (320, 240), "white")
        draw = ImageDraw.Draw(frame)
        if shape == "circle":
            draw.ellipse((72, 32, 248, 208), fill=color)
        elif shape == "square":
            draw.rectangle((72, 32, 248, 208), fill=color)
        else:
            draw.polygon(((160, 28), (60, 206), (260, 206)), fill=color)
        images.append(frame)
    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], loop=0,
                   duration=[duration for _, _, duration in scenes])
    return buffer.getvalue()


def decode_clip(raw):
    frames = []
    elapsed = 0
    with Image.open(io.BytesIO(raw)) as clip:
        for number in range(clip.n_frames):
            clip.seek(number)
            buffer = io.BytesIO()
            clip.convert("RGB").save(buffer, format="PNG")
            frames.append((elapsed / 1000, buffer.getvalue()))
            elapsed += clip.info["duration"]
    return elapsed / 1000, frames


class Archive:
    def __init__(self):
        self.requests = []
        self.clips = {f"/media/p{part}.gif": encode_clip(scenes) for part, scenes in PARTS.items()}

    def serve(self, request):
        self.requests.append((request.method, str(request.url)))
        if request.method != "GET" or str(request.url) not in {SOURCE, *(ORIGIN + path for path in self.clips)}:
            raise ReplayMismatch("unexpected_archive_request")
        if str(request.url) == SOURCE:
            return httpx.Response(200, text=NOTE)
        return httpx.Response(200, content=self.clips[request.url.path], headers={"Content-Type": "image/gif"})


class LocalVideo:
    def __init__(self, archive, operations):
        self.archive, self.operations = archive, operations
        self.discovery = SimpleNamespace(history_scope=lambda: ("rehearsal", "whale", 1))
        self.adapter = self.repo = self
        self.jobs = {}
        self.observations = {}

    async def _read(self, reader, bvid):
        return await reader(bvid)

    async def video_metadata(self, bvid):
        if bvid != BV:
            raise ReplayMismatch("unbound_video")
        return {"bvid": BV, "parts": [{"page": part, "duration": sum(scene[2] for scene in scenes) / 1000}
                                     for part, scenes in PARTS.items()]}

    def round(self, identity):
        return self.jobs.get(identity)

    def requested_preview(self, identity, character, selection, account):
        row = self.round(identity)
        if row and row["config"]["operator_selection"] != selection:
            raise ReplayMismatch("changed_selection")
        return row

    async def queue_full_video(self, identity, character, selection):
        if selection["bvid"] != BV or not selection["ranges"] or any(r["part"] != 2 for r in selection["ranges"]):
            raise ReplayMismatch("wrong_part")
        self.jobs[identity] = {"config": {"operator_selection": selection}, "status": "running",
                               "job": asyncio.create_task(self.observe(selection))}

    async def observe(self, selection):
        response = await self.archive.get("/media/p2.gif")
        duration, frames = decode_clip(response.raise_for_status().content)
        intervals = []
        for interval in selection["ranges"]:
            if not 0 <= interval["start"] < interval["end"] <= duration:
                raise ReplayMismatch("range_outside_media")
            highlights = []
            for index, (seconds, image) in enumerate(frames):
                if not interval["start"] <= seconds < interval["end"]:
                    continue
                encoded = base64.b64encode(image).decode("ascii")
                try:
                    observed = await self.operations.generate_json(
                        [{"role": "user", "content": [
                            {"type": "text", "text": "识别白底上唯一有色几何图形。color 用英文小写颜色名，shape 用英文小写图形名。"},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                        ]}],
                        {"type": "object", "properties": {"color": {"type": "string"}, "shape": {"type": "string"}},
                         "required": ["color", "shape"], "additionalProperties": False},
                        max_tokens=256,
                    )
                except ValueError as error:
                    raise ReplayMismatch(f"invalid_visual_receipt_at_{seconds:g}s") from error
                color, shape, _ = PARTS[2][index]
                if observed != {"color": color, "shape": shape}:
                    raise ReplayMismatch(f"visual_observation_at_{seconds:g}s")
                self.observations[seconds] = observed
                highlights.append({"start": seconds, "kind": "实际解码画面", "text": f"{color} {shape}",
                                   "source_url": f"https://www.bilibili.com/video/{BV}/?p=2&t={int(seconds)}"})
            intervals.append(interval | {"highlights": highlights})
        return {"status": "completed", "selection": selection, "comments": {}, "error": None,
                "coverage": {"viewing": "visual_only", "parts": [{"part": 2, "cid": "local-p2", "ranges": intervals}]}}

    def full_video_status(self, row):
        return row["job"].result() if row["job"].done() else {"status": "running"}

    async def close(self):
        jobs = [row["job"] for row in self.jobs.values()]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


def validate_delivery(task, edition, observations):
    revision = task["revision"]
    plan = task["plan"] or {}
    if (task["status"] != "completed" or not plan.get("candidate") or not edition
            or edition["task_revision"] != revision or edition["body"] != task["message"]):
        raise ReplayMismatch(f"r{revision}_undelivered_{task['status']}")
    frames, pages = {}, set()
    for step in task["steps"]:
        receipt = step.get("result") or {}
        if not step.get("call_id") or not receipt:
            continue
        if step["tool"] == "inspect_video":
            for index, frame in enumerate(receipt.get("findings", []), 1):
                seconds = frame["seconds"]
                observed = observations.get(seconds)
                if (not observed or frame["part"] != 2 or frame["cid"] != "local-p2"
                        or frame["text"] != f"{observed['color']} {observed['shape']}"
                        or frame["url"] != f"https://www.bilibili.com/video/{BV}/?p=2&t={int(seconds)}"):
                    raise ReplayMismatch("unverified_frame")
                frames[f"{step['id']}:{index}"] = seconds
        if step["tool"] == "web_page" and receipt.get("url") == SOURCE and receipt.get("excerpt") == NOTE:
            pages.add(step["id"])
    selected = plan.get("selected", {})
    prose = "\n".join([*(item["title"] + " " + item["rationale"] for item in selected.get("items", [])),
                      *selected.get("gaps", [])])
    if re.search(r"红色|红圆|圆形|圆圈|圆盘|\b(?:red|circle)\b", prose, re.I):
        raise ReplayMismatch(f"r{revision}_spoiler_in_prose")
    chosen = []
    for item in selected.get("items", []):
        refs = set(item["sources"])
        times = {frames[key] for key in refs if key in frames}
        if len(times) != 1 or not refs & pages or refs - frames.keys() - pages:
            raise ReplayMismatch(f"r{revision}_missing_or_mixed_sources")
        second = times.pop()
        if not any(name in item["title"].lower() for name in NAMES.get(second, ())):
            raise ReplayMismatch(f"r{revision}_title_does_not_identify_frame")
        text = item["title"] + " " + item["rationale"]
        times_in_text = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*(?:秒|seconds?\b)", text, re.I)]
        times_in_text.extend(60 * int(minutes) + float(seconds)
                             for minutes, seconds in re.findall(r"(?<!\d)(\d+):([0-5]\d(?:\.\d+)?)(?!\d)", text))
        urls = [url.rstrip(".,;!?") for url in re.findall(r"https?://[^\s<>；，。！？、）)\]]+", text)]
        if (any(value != second for value in times_in_text) or
                any(url not in {SOURCE, f"https://www.bilibili.com/video/{BV}/?p=2&t={int(second)}"} for url in urls)):
            raise ReplayMismatch(f"r{revision}_unverified_inline_citation")
        chosen.append(second)
    if sorted(chosen) != sorted(EXPECTED[revision]):
        raise ReplayMismatch(f"r{revision}_wrong_or_duplicate_clips")
    if revision == 2 and not any(re.search(r"不足|不够|仅.{0,6}[一1]|只.{0,6}[一1]|[无没缺未找凑].{0,12}[二两2]|only one|no second", gap, re.I)
                                 for gap in selected.get("gaps", [])):
        raise ReplayMismatch("r2_missing_shortfall")
    known = evidence_index(task["steps"])
    if any(known[key] not in edition["body"] for item in selected["items"] for key in item["sources"]):
        raise ReplayMismatch("edition_missing_citation")
    return {"revision": revision, "selected_seconds": chosen, "edition": edition["body"]}


class PrivateChat(OperatorChat):
    async def _action(self, intent, *args, **options):
        if intent.action not in {"agent_task", "steer_task", "clarify"}:
            raise ReplayMismatch(f"unexpected_chat_action/{intent.action}")
        return await super()._action(intent, *args, **options)


async def exercise(store, omni, operations, responses, archive, transport, *, token):
    seeds = json.loads((ROOT / "config/characters.json").read_text(encoding="utf-8"))
    character = next(seed for seed in seeds if seed["id"] == "whale") | {"version": 1}
    runtime = SimpleNamespace(store=store, character=character, llm=omni, operation_llm=operations, operator_generation=nullcontext,
                              offline_generation=nullcontext, chat_routing_generation=nullcontext,
                              settings=SimpleNamespace(data={"omni": omni.config}, responses_config=responses.config.model_dump(),
                                                      provider_key=lambda name: token if name == "omni" else None),
                              daily_memory=SimpleNamespace(preference=lambda scope: {"enabled": False}),
                              context=SimpleNamespace(record=lambda *args, **kwargs: None),
                              validate_memory_delivery=lambda *args, **kwargs: None)
    video = LocalVideo(archive, operations)
    video.policy = lambda: {"business_timezone": "Asia/Taipei"}
    tasks = runtime.tasks = create_task_runtime(runtime, store, video)
    runtime.memory_organizer = SimpleNamespace(scope=tasks.scope, schedule=lambda: None)
    chat = PrivateChat(runtime, store, video, SimpleNamespace())

    async def local_page(request, args):
        if args.web_fetch != SOURCE or SOURCE not in request.goal:
            raise ReplayMismatch("unknown_source")
        response = await archive.get(SOURCE)
        return {"url": SOURCE, "title": TITLE, "excerpt": response.raise_for_status().text, "truncated": False}

    tasks.tools = {"inspect_video": tasks.tools["inspect_video"], "web_page": replace(tasks.tools["web_page"], run=local_page)}
    tasks.responses_client = lambda: responses.create
    identity = uuid4()
    editions, initial_reads = [], []
    tasks.start()
    try:
        for revision, message in enumerate((REQUEST, CHANGE), 1):
            result = await chat.send(ChatTurn(request_id=identity if revision == 1 else uuid4(), message=message))
            card = result.get("card") or {}
            data = card.get("data") or {}
            if (card.get("type"), data.get("id"), data.get("revision")) != ("agent_task", str(identity), revision):
                raise ReplayMismatch(f"r{revision}_ordinary_chat_did_not_bind_task")
            while (task := tasks.current(str(identity)))["status"] in {"queued", "running", "waiting", "scheduled"}:
                await asyncio.sleep(0.1)
            for row in video.jobs.values():
                if row["job"].done():
                    row["job"].result()
            if task["revision"] != revision or message not in task["goal"]:
                raise ReplayMismatch(f"r{revision}_owner_instruction_not_persisted")
            editions.append(validate_delivery(task, tasks.workspace.latest(str(identity)), video.observations))
            if revision == 1:
                initial_reads = transport.requests.copy()
        if (len(tasks.activity()) != 1 or transport.requests != initial_reads
                or set(initial_reads) != {("GET", ORIGIN + "/media/p2.gif"), ("GET", SOURCE)}):
            raise ReplayMismatch("unexpected_reads_or_failed_reuse")
        return {"status": "passed", "editions": editions,
                "media": {"sample": "synthetic GIF P2", "local_uri": ORIGIN + "/media/p2.gif",
                          "time_unit": "decoded scene start in seconds", "omni_frame_observations": video.observations},
                "text_source": {"url": SOURCE, "fixture_text": NOTE}, "archive_requests": transport.requests,
                "platform_writes": 0, "boundary": BOUNDARY}
    finally:
        await tasks.close()
        await video.close()


async def run(env):
    config = connection(env)
    if config is None:
        return {"status": "skipped", "reason": "missing_studio_service_configuration", "boundary": BOUNDARY}
    root, token = config
    scratch = ROOT / "tmp"
    scratch.mkdir(exist_ok=True)
    async with AsyncExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="creative-p2-", dir=scratch))
        store = Store(Path(directory) / "replay.sqlite3", [])
        stack.callback(store.close)
        front = {"kind": "openai_compatible", "base_url": root + "/v1", "model": env.get("OMNI_MODEL_ALIAS", "omni-default"),
                 "context_source": "model_card", "prompt_token_endpoint": True, "structured_output": "structural_tag",
                 "max_tokens": 4096}
        omni = APIProvider(front, token)
        stack.push_async_callback(omni.close)
        responses = await stack.enter_async_context(ResponsesClient(
            {"base_url": root + "/llm/v1", "model": env.get("OMNI_LLM_MODEL_ALIAS", "llm-default")}, token))
        operations = APIProvider(operation_profile(omni.config | {"responses_model": responses.config.model}),
                                 token, background=True)
        stack.push_async_callback(operations.close)
        transport = Archive()
        archive = await stack.enter_async_context(httpx.AsyncClient(base_url=ORIGIN, transport=httpx.MockTransport(transport.serve)))
        async with asyncio.timeout(600):
            return await exercise(store, omni, operations, responses, archive, transport, token=token)


def main():
    try:
        report = asyncio.run(run(os.environ))
    except ReplayMismatch as error:
        report = {"status": "failed", "check": str(error), "boundary": BOUNDARY}
    except Exception as error:
        report = {"status": "failed", "category": type(error).__name__, "boundary": BOUNDARY}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["status"] == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
