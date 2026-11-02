"""CC-first requests may add one budgeted observation to the same task."""

import asyncio
import base64
import re

import pytest
from test_browsing import browsing as browsing
from test_browsing import finished, model_packet

from tuber_studio.generation_meter import GenerationMeter, reserve_generation, settle_generation


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


def install_visual(service, *, ask=True, ask_again=False):
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jHtkAAAAASUVORK5CYII="
    )
    visual = FollowupVisual(service.runtime)
    visual.prompts = []
    service.evidence.visual = visual
    service.store.set("browsing_policy", service.policy() | {"visual_enabled": True})

    async def frames(metadata, count):
        service.adapter.calls.append(("frames", count))
        return [
            {
                "id": "11:5",
                "cid": "11",
                "actual_time": 5,
                "requested_time": 6,
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
        if schema["title"] != "ContentDecision":
            return await original(messages, schema, max_tokens=max_tokens)
        data = model_packet(messages[-1]["content"])
        visual.prompts.append(
            "\n".join(message["content"] for message in messages if message["role"] == "system")
        )
        inputs.append(data)
        visual_text = data["content"].partition("画面观察\n")[2]
        visual_ids = re.findall(r"(?m)^\[(e\d+)\|", visual_text)
        if ask and (not visual_ids or ask_again):
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
    visual, inputs = install_visual(browsing, ask_again=ask_again)
    row = await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert row["status"] == "completed" and visual.generated == 1 and len(inputs) == 2
    assert inputs[0]["segments"] == [{"id": "e1"}]
    assert "[e1|00:00]加盐之后，鸡蛋浮了起来。" in inputs[0]["content"]
    assert [segment["id"] for segment in inputs[1]["segments"]] == ["e1", "e2"]
    assert inputs[1]["segments"][0] == inputs[0]["segments"][0]
    assert "[e2|00:05]" in inputs[1]["content"]
    assert inputs[1]["content"].count("P1") == 1
    assert "parts" not in inputs[1]
    assert inputs[0]["has_visual"] is False and inputs[0]["source_kinds"] == ["subtitle"]
    assert inputs[1]["has_visual"] is True
    assert inputs[1]["source_kinds"] == ["subtitle", "visual_observation"]
    assert inputs[1]["coverage"] == "sampled_visual"
    assert "coverage_required表示还要补充的内容" in visual.prompts[1]
    assert "interest=like表示想参与这个话题，吐槽、共情和分享也属于参与" in visual.prompts[1]
    assert "worth_commenting表示有具体想说的话" in visual.prompts[1]
    assert "当前材料够用选provided" in visual.prompts[1]
    assert "需新的画面选visual" in visual.prompts[1]
    assert visual.task["round_id"] == row["id"] and visual.task["id"] == task["id"]
    assert visual.deadline <= row["deadline"]
    assert task["input_version"] == task["material_key"]
    assert task["material_key"] != task["visual_followup"]["source_material_key"]
    assert task["visual_followup"]["needs_visual_again"] is ask_again
    assert task["visual_followup"]["decisions_before"][0]["coverage_required"] == "visual"
    assert task["visual_followup"]["decisions_before"][0]["evidence_ids"] == ["11:0"]
    if ask_again:
        assert task["status"] == "viewed_without_comment" and not browsing.intents()
    else:
        assert browsing.intents()[0]["evidence"] == ["frame:11:5"]
        assert browsing.intents()[0]["material_keys"] == [task["material_key"]]
    assert len([call for call in browsing.adapter.calls if call[0] == "subtitles"]) == 1
    assert not browsing.adapter.sent


async def test_no_request_keeps_zero_visual_generations(browsing):
    visual, inputs = install_visual(browsing, ask=False)
    await finished(browsing, preview=True)
    assert visual.generated == 0 and len(inputs) == 1
    assert not any(call[0] == "frames" for call in browsing.adapter.calls)


async def test_visual_request_respects_disabled_switch(browsing):
    visual, inputs = install_visual(browsing)
    browsing.store.set("browsing_policy", browsing.policy() | {"visual_enabled": False})
    await finished(browsing, preview=True)
    assert visual.generated == 0 and len(inputs) == 1
    assert browsing.tasks()[0]["visual_followup"]["status"] == "disabled"
    assert not browsing.intents()


async def test_exhausted_parent_budget_never_generates_followup(browsing):
    visual, inputs = install_visual(browsing)
    browsing.store.set("browsing_policy", browsing.policy() | {"max_model_calls": 1})
    row = await finished(browsing, preview=True)
    task = browsing.tasks()[0]
    assert row["budget"]["model_calls"] == 1 and visual.generated == 0
    assert row["status"] == "partial" and task["status"] == "failed"
    assert task["visual_followup"]["status"] == "failed" and len(inputs) == 1
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
