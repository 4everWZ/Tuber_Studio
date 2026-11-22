import base64
import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from tuber_studio.agent.tasks.clip_review import review_clip_window


@pytest.fixture(scope="module")
def saved_clip(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("requires the saved-clip decoder and encoder")
    path = tmp_path_factory.mktemp("saved-review") / "window.mp4"
    frames = []
    for i in range(30):
        image = Image.new("RGB", (320, 180), "#203020" if i < 23 else "#193bbb")
        if i == 27:
            ImageDraw.Draw(image).text(
                (67, 140), "THE BRIDGE FELL", fill="white", font=ImageFont.load_default(size=22)
            )
        frames.append(image.tobytes())
    # Frame 11 is held for four ticks; neither the cut nor the flash has
    # a PTS derivable from frame number / nominal fps.
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            "320x180",
            "-r",
            "20",
            "-i",
            "pipe:0",
            "-vf",
            r"setpts=PTS+3*gte(N\,12)",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+empty_moov+frag_keyframe+default_base_moof",
            str(path),
        ],
        input=b"".join(frames),
        check=True,
    )
    return path


async def test_late_cut_and_single_frame_caption_survive_vfr_review(saved_clip):
    result = await review_clip_window(saved_clip, start=10, end=11.65, expected_frames=30)
    coverage = result["coverage"]
    assert coverage["complete"] and coverage["uncovered"] == []
    assert coverage["scanned_frames"] == 30
    samples = coverage["samples"]
    assert [samples[0]["frame"], samples[-1]["frame"]] == [0, 29]
    selected = {}
    for sample, part in zip(samples, result["images"], strict=True):
        prefix, payload = part["image_url"]["url"].split(",")
        assert prefix == "data:image/jpeg;base64"
        with Image.open(io.BytesIO(base64.b64decode(payload))) as jpeg:
            selected[sample["frame"]] = jpeg.convert("RGB")
    changes = {item["frame"]: item for item in coverage["changes"]}
    assert "scene_change" in changes[23]["reasons"]
    assert changes[23]["seconds"] == pytest.approx(11.30)
    assert {27, 28} <= {i for i, item in changes.items() if "lower_text_change" in item["reasons"]}
    assert next(s["seconds"] for s in samples if s["frame"] == 27) == pytest.approx(11.50)
    assert selected[0].getpixel((10, 10))[2] < 60
    assert selected[23].getpixel((10, 10))[2] > 160
    for i, image in selected.items():
        whites = image.convert("L").crop((50, 130, 290, 178)).histogram()[200:]
        assert (sum(whites) > 100) == (i == 27)


async def test_unrepresented_state_and_unverified_window_fail_closed(saved_clip):
    result = await review_clip_window(saved_clip, start=10, end=11.65, expected_frames=30, max_images=2)
    coverage = result["coverage"]
    assert not coverage["complete"]
    assert [s["frame"] for s in coverage["samples"]] == [0, 29]
    assert any(gap["start"] <= 11.50 < gap["end"] for gap in coverage["uncovered"])
    assert "缩短" in coverage["note"]

    for frames, end, budget in [(29, 11.65, 6), (30, 11.5, 6), (30, 11.65, 1)]:
        result = await review_clip_window(
            saved_clip, start=10, end=end, expected_frames=frames, max_images=budget
        )
        assert not result["coverage"]["complete"] and not result["images"]
        assert result["coverage"]["uncovered"] == [{"start": 10, "end": end}]


@pytest.fixture(scope="module")
def busy_clip(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("requires the saved-clip decoder and encoder")
    path = tmp_path_factory.mktemp("many-cuts") / "window.mp4"
    frames = []
    for i in range(20):
        image = Image.new("RGB", (160, 90), "blue" if i % 2 else "red")
        if i == 18:
            ImageDraw.Draw(image).text((15, 62), "THE BRIDGE FELL", fill="white")
        frames.append(image.tobytes())
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            "160x90",
            "-r",
            "20",
            "-i",
            "pipe:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        input=b"".join(frames),
        check=True,
    )
    return path


@pytest.mark.parametrize("narrative", [False, True])
async def test_full_screening_passes_all_batches_through_both_gates(busy_clip, monkeypatch, narrative):
    import json

    from tuber_studio.agent.tasks import clip_review
    from tuber_studio.agent.tasks.delivery import NON_NARRATIVE, PROGRAMME_CONTEXT, accepted_window
    from tuber_studio.agent.tasks.tools import screen_clip_window

    monkeypatch.setattr(clip_review, "_RGB_BATCH_BYTES", 4 * 160 * 90 * 3)
    seen = []
    clip = {"start": 8, "end": 9, "frames": 20}

    async def generate(messages, schema, *, max_tokens):
        content = messages[-1]["content"]
        assert max_tokens == 512
        if isinstance(content, str):
            assert json.loads(content) == {"待标注原句": {"1": "THE BRIDGE FELL"}}
            return {
                "1": {
                    "topic": NON_NARRATIVE[0],
                    "subject": "提及片中人物或所讲世界中发生的事" if narrative else PROGRAMME_CONTEXT,
                }
            }
        from tuber_studio.infra.inference.http_api import InferenceBudgetExceeded

        if len(content[1::2]) > 2:
            raise InferenceBudgetExceeded("service rejected this image batch")
        replies = {}
        for header, pixels in zip(content[:-1:2], content[1::2], strict=True):
            position = json.loads(header["text"])
            with Image.open(io.BytesIO(base64.b64decode(pixels["image_url"]["url"].split(",")[1]))) as image:
                red, _, blue = image.getpixel((5, 5))
                assert (red > blue) == (len(seen) % 2 == 0)
                letters = sum(image.convert("L").crop((10, 58, 144, 85)).histogram()[180:]) > 5
            seen.append(position["seconds"])
            replies[str(position["frame"])] = {
                "scene": "符合要求",
                "captions": "已读且符合" if letters else "无烧录字幕",
                "screen_text": "THE BRIDGE FELL" if letters else "",
                "basis": "已读取该帧",
            }
        return replies

    result = await screen_clip_window((busy_clip, clip), "不剧透", generate, 512, True)
    coverage = result["coverage"]
    assert coverage["complete"] and coverage["uncovered"] == []
    assert [b["count"] for b in coverage["batches"]] == [4] * 5
    assert [b["count"] for b in coverage["model_batches"]] == [2] * 10
    assert coverage["model_budget_splits"] == [
        {
            "keys": [0, 1, 2, 3],
            "error": "InferenceBudgetExceeded",
            "message": "service rejected this image batch",
        }
    ]
    assert seen == pytest.approx([8 + n / 20 for n in range(20)])
    assert [o["frame"] for o in result["observations"]] == list(range(20))
    assert [o["frame"] for o in result["observations"] if o["screen_text"]] == [18]
    assert accepted_window(result, clip, True) == (not narrative)


async def test_single_frame_service_budget_error_is_not_a_content_rejection(busy_clip):
    from tuber_studio.agent.tasks.tools import screen_clip_window
    from tuber_studio.infra.inference.http_api import InferenceBudgetExceeded

    async def denied(*args, **kwargs):
        raise InferenceBudgetExceeded("actual admission error")

    with pytest.raises(InferenceBudgetExceeded, match="actual admission"):
        await screen_clip_window((busy_clip, {"start": 0, "end": 1, "frames": 20}), "不剧透", denied, 512)


async def test_caption_batch_retries_only_actual_capacity_errors():
    from tuber_studio.agent.tasks.delivery import NON_NARRATIVE, PROGRAMME_CONTEXT
    from tuber_studio.agent.tasks.tools import classify_clip_captions
    from tuber_studio.infra.inference.http_api import InferenceBudgetExceeded

    calls = []

    async def limited(messages, schema, **kwargs):
        keys = schema["required"]
        calls.append(keys)
        if len(keys) > 2:
            raise InferenceBudgetExceeded("actual service limit")
        return {k: {"topic": NON_NARRATIVE[0], "subject": PROGRAMME_CONTEXT} for k in keys}

    obs = [{"screen_text": s} for s in ("甲", "乙", "丙", "丁")]
    result = await classify_clip_captions({"observations": obs}, "不剧透", limited, 512, True)
    assert result["accepted"] and calls == [["1", "2", "3", "4"], ["1", "2"], ["3", "4"]]
    assert [t["text"] for t in result["caption_topics"]] == ["甲", "乙", "丙", "丁"]
    assert result["caption_budget_splits"][0]["keys"] == [1, 2, 3, 4]


async def test_missing_frame_verdict_does_not_retry_or_pass(busy_clip):
    from tuber_studio.agent.tasks.delivery import DelegationError
    from tuber_studio.agent.tasks.tools import screen_clip_window

    calls = 0

    async def missing(messages, schema, **kwargs):
        nonlocal calls
        calls += 1
        return {}

    with pytest.raises(DelegationError, match="未逐帧绑定"):
        await screen_clip_window((busy_clip, {"start": 0, "end": 1, "frames": 20}), "不剧透", missing, 512)
    assert calls == 1


def test_resume_reopens_only_unseen_preview_exclusions():
    from tuber_studio.agent.tasks.tools import retry_incomplete_previews

    unseen = {
        "accepted": False,
        "clip": {"frames": 75},
        "observations": [],
        "coverage": {
            "complete": False,
            "scanned_frames": 75,
            "samples": [{"frame": 0}],
            "uncovered": [{"start": 1, "end": 2}],
        },
    }
    rejected = unseen | {"observations": [{"scene": "不符合要求"}]}
    plan = {
        "window_reviews": {"unseen": unseen, "rejected": rejected},
        "excluded_video_frames": ["unseen:1", "rejected:1", "unseen:2", "other:1"],
    }
    retry_incomplete_previews(plan)
    retry_incomplete_previews(plan)
    assert plan == {
        "window_reviews": {"rejected": rejected},
        "excluded_video_frames": ["rejected:1", "other:1"],
    }
