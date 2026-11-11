"""An initial subtitle and frame read can be followed by one targeted observation."""

import asyncio
import base64
import re

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet

from tuber_studio.generation_meter import GenerationMeter, reserve_generation, settle_generation
from tuber_studio.operations.browsing.models import DisabledVideoEvidence


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


def install_visual(service, *, ask=True, ask_again=False, initial_time=8):
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jHtkAAAAASUVORK5CYII="
    )
    visual = FollowupVisual(service.runtime)
    visual.prompts = []
    service.evidence.visual = visual
    async def frames(metadata, count, *, cid=None, timestamps=None):
        service.adapter.calls.append(("frames", count))
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

    service.adapter.video_frames = frames
    original = service.runtime.llm.generate_json
    inputs = []

    async def generate(messages, schema, *, max_tokens):
        if schema["title"] == "ResponseDecision":
            result = await original(messages, schema, max_tokens=max_tokens)
            latest_frame = re.findall(r"(?m)^\[(ref:\d+)\|[^\]]+\]P\d+ 画面", messages[-1]["content"])
            if latest_frame:
                result["evidence_ids"] = latest_frame[-1:]
            return result
        if schema["title"] != "ContentDecision":
            return await original(messages, schema, max_tokens=max_tokens)
        data = model_packet(messages[-1]["content"])
        visual.prompts.append(
            "\n".join(message["content"] for message in messages if message["role"] == "system")
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
        return await original(messages, schema, max_tokens=max_tokens)

    service.runtime.llm.generate_json = generate
    return visual, inputs


@pytest.mark.parametrize("ask_again", [False, True])
async def test_cc_visual_request_adds_once_then_rejudges_same_task(browsing, ask_again):
    # The replay mock has no completion usage, so reserve room for all three
    # full-budget calls instead of changing the new per-call default.
    browsing.store.set("browsing_policy", browsing.policy() | {"max_output_tokens": 32768})
    visual, inputs = install_visual(browsing, ask_again=ask_again)
    row = await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert row["status"] == "completed" and visual.generated == 2 and len(inputs) == 2
    assert [segment["id"] for segment in inputs[0]["segments"]] == ["e1", "e2"]
    assert "[e1|00:00]加盐之后，鸡蛋浮了起来。" in inputs[0]["content"]
    assert [segment["id"] for segment in inputs[1]["segments"]] == ["e1", "e2", "e3"]
    assert inputs[1]["segments"][0] == inputs[0]["segments"][0]
    assert "[e2|00:08]" in inputs[1]["content"]
    assert "[e3|00:05]" in inputs[1]["content"]
    assert inputs[1]["content"].count("P1") == 1
    assert "parts" not in inputs[1]
    assert inputs[0]["has_visual"] and inputs[0]["source_kinds"] == ["subtitle", "visual_observation"]
    assert inputs[1]["has_visual"] is True
    assert inputs[1]["source_kinds"] == ["subtitle", "visual_observation"]
    assert inputs[1]["coverage"] == "sampled_visual"
    assert all(field in visual.prompts[1] for field in ("source_kinds", "has_visual", "coverage_required"))
    assert "需要补看时选visual" in visual.prompts[0]
    assert "需要补看时选visual" not in visual.prompts[1]
    assert "本次取样画面已到齐" in visual.prompts[1]
    assert visual.task["round_id"] == row["id"] and visual.task["id"] == task["id"]
    assert visual.deadline <= row["deadline"]
    assert task["input_version"] == task["material_key"]
    assert task["material_key"] != task["visual_followup"]["source_material_key"]
    assert task["visual_followup"]["decisions_before"][0]["coverage_required"] == "visual"
    assert task["visual_followup"]["decisions_before"][0]["evidence_ids"] == ["11:0"]
    if ask_again:
        assert task["status"] == "viewed_without_comment" and not browsing.intents()
    else:
        assert "frame:11:5" in browsing.intents()[0]["evidence"]
        assert browsing.intents()[0]["material_keys"] == [task["material_key"]]
    assert len([call for call in browsing.adapter.calls if call[0] == "subtitles"]) == 1
    assert not browsing.adapter.sent


async def test_no_followup_keeps_initial_visual_generation(browsing):
    visual, inputs = install_visual(browsing, ask=False)
    await finished(browsing, preview=True)
    assert visual.generated == 1 and len(inputs) == 1


async def test_followup_reuses_an_already_observed_frame(browsing):
    visual, inputs = install_visual(browsing, initial_time=5)
    await finished(browsing, preview=True)
    assert visual.generated == 1 and len(inputs) == 1
    assert browsing.tasks()[0]["material_mode"] == "subtitle_visual"


async def test_followup_uses_the_cited_part_and_preserves_only_reply_citations(browsing):
    browsing.store.set("browsing_policy", browsing.policy() | {"max_output_tokens": 32768})
    visual, _ = install_visual(browsing)
    metadata = browsing.adapter.video_metadata
    subtitles = browsing.adapter.subtitles
    frames = browsing.adapter.video_frames
    generate = browsing.runtime.llm.generate_json
    frame_requests = []
    response_inputs = []

    async def two_parts(video):
        data = await metadata(video)
        data["parts"].append({"cid": "22", "title": "第二P", "duration": 120})
        return data

    async def later_subtitles(video, cid, languages):
        data = await subtitles(video, cid, languages)
        if cid == "22":
            data["segments"][0].update(start=60, end=70, text="第二P的字幕。")
        return data

    async def part_frames(metadata, count, *, cid=None, timestamps=None):
        frame_requests.append((cid, timestamps))
        result = await frames(metadata, count)
        if cid == "22":
            result[0].update(id="22:65", cid="22", actual_time=65, requested_time=65)
        return result

    async def cite_second_part(messages, schema, *, max_tokens):
        result = await generate(messages, schema, max_tokens=max_tokens)
        if schema["title"] == "ContentDecision" and result["coverage_required"] == "visual":
            result["evidence_ids"] = ["e2"]
        if schema["title"] == "ResponseDecision":
            data = model_packet(messages[-1]["content"])
            response_inputs.append(data["content"])
            result["evidence_ids"] = re.findall(r"(?m)^\[(ref:\d+)\|[^\]]+\]P2 画面", data["content"])
        return result

    browsing.adapter.video_metadata = two_parts
    browsing.adapter.subtitles = later_subtitles
    browsing.adapter.video_frames = part_frames
    browsing.runtime.llm.generate_json = cite_second_part
    await finished(browsing, preview=True)

    assert frame_requests == [(None, None), ("22", [65.0])]
    assert visual.generated == 2
    assert len(response_inputs) == 1
    assert "P2 画面 蓝色液体位于透明杯中。" in response_inputs[0]
    assert "P2 字幕 第二P的字幕。" in response_inputs[0]
    assert browsing.intents()[0]["evidence"] == ["frame:22:65"]


async def test_missing_visual_provider_reads_subtitles_without_followup(browsing):
    visual, inputs = install_visual(browsing)
    browsing.evidence.visual = DisabledVideoEvidence()
    await finished(browsing, preview=True)
    assert visual.generated == 0 and len(inputs) == 1
    assert browsing.tasks()[0]["material_mode"] == "subtitle"
    assert not browsing.intents()


async def test_exhausted_parent_budget_never_generates_followup(browsing):
    visual, inputs = install_visual(browsing)
    browsing.store.set("browsing_policy", browsing.policy() | {"max_model_calls": 2})
    row = await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert row["budget"]["model_calls"] == 2 and visual.generated == 1
    assert row["status"] == "partial" and task["status"] == "checkpoint"
    assert task["payload"]["retry_kind"] == "round_budget"
    assert task["visual_followup"]["status"] == "requested" and len(inputs) == 1
    assert task["decisions"][0]["coverage_required"] == "visual"
    assert not browsing.intents() and not browsing.adapter.sent


async def test_cancel_collects_visual_and_original_parent(browsing):
    visual, _ = install_visual(browsing)
    visual.gate = asyncio.Event()
    started = await browsing.run(preview=True)
    await asyncio.wait_for(visual.started.wait(), 2)
    await browsing.stop()
    row = browsing.repo.round(started["id"])
    assert row["status"] == "cancelled" and not browsing.evidence.pending
    assert visual.cancelled and visual.task["cancel_signal"].is_set()
    assert not browsing.intents() and not browsing.adapter.sent
