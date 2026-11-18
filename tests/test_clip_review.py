import base64
import io
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw, ImageFont

from tuber_studio.agent.clip_review import review_clip_window


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
