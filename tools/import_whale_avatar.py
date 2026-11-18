"""Extract the attributed full-body Q-style atlas into the studio's GIF states.

Run with `uv run --with pillow python tools/import_whale_avatar.py`.
Only atlas extraction, scaling and GIF encoding are performed; no new art is drawn.
Raw downloads remain in ignored tmp/, not in the program image.
"""

from __future__ import annotations

import argparse
import json
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
REVISION = "7661c8b304c5400701f91da01b1a643a207331de"
REPOSITORY = "https://github.com/YunYueSama/codex-deepseek-pet"
RAW = f"https://raw.githubusercontent.com/YunYueSama/codex-deepseek-pet/{REVISION}"
CLIPS = {
    "idle": ("dense-idle", [1, 3, 5, 7, 5, 3, 1], [200, 240, 260, 800, 260, 240, 600]),
    "talking": ("dense-wave", list(range(1, 32, 2)), [100] * 15 + [650]),
    "cheerful": ("actions", [0, 10, 15, 10, 0], [300, 200, 500, 200, 400]),
    "angry": ("actions", [0, 8, 8, 4, 0], [300, 200, 700, 300, 400]),
    "confused": ("expressions", [0, 1, 2, 3, 2, 1, 0], [300, 180, 250, 650, 250, 180, 500]),
    "gift": ("actions", [0, 5, 5, 1, 0], [250, 200, 700, 500, 350]),
}

REACTIONS = {
    "dance": ("摇摆一下", ["cheerful"], list(range(1, 32, 2)), 90),
    "eat": ("开饭啦", ["cheerful", "excited"], list(range(1, 32, 2)), 110),
    "jump": ("蹦起来", ["excited"], [0, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29, 31], 80),
    "sleep": ("困了，晚安", ["idle"], [1, 3, 5, 7, 9, 11, 13, 17, 19, 21, 23, 25, 27, 29, 31], 160),
    "sneak": ("悄悄偷吃", ["shy", "cheerful"], [1, 3, 5, 7, 9, 11, 15, 17, 19, 21, 23, 25, 27, 29, 13, 31], 100),
    "walk": ("这就来", ["idle", "cheerful"], [0, 16, 5, 6, 22, 10, 26, 13, 14, 30], 100),
}


def write_clip(atlas, indices, durations, path, size):
    frames = []
    for index in indices:
        left, top = index % 4 * 512, index // 4 * 512
        tile = atlas.crop((left, top, left + 512, top + 512)).resize((size, size), Image.Resampling.LANCZOS)
        frame = tile.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
        frame.paste(255, mask=tile.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0))
        frame.info["transparency"] = 255
        frames.append(frame)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   disposal=2, transparency=255, optimize=False)


def build_reactions():
    folder = ROOT / "assets/whale/memes"
    manifest = folder / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entries = {entry["id"]: entry for entry in data["entries"]}
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for action, (title, emotions, indices, duration) in REACTIONS.items():
            asset = f"assets/whale/dense-{action}.png"
            identity = "motion-" + action
            response = client.get(f"{RAW}/{asset}")
            response.raise_for_status()
            with Image.open(BytesIO(response.content)) as atlas:
                if atlas.size != (2048, 4096):
                    raise ValueError(f"Unexpected atlas dimensions for {action}")
                write_clip(atlas.convert("RGBA"), indices, duration, folder / f"{identity}.gif", 256)
            entries[identity] = {"id": identity, "title": title, "path": f"{identity}.gif", "emotions": emotions,
                                 "source": f"{REPOSITORY}/blob/{REVISION}/{asset}",
                                 "note": f"原动作图集按帧 {indices}（行优先，从 0 编号）裁切，缩放为 256×256 透明循环 GIF，每帧 {duration} ms。"}
    data["entries"] = list(entries.values())
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(singing_only=False):
    scratch = ROOT / "tmp/whale-avatar-import"
    scratch.mkdir(parents=True, exist_ok=True)
    atlases = {}
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for name in ["expressions"] if singing_only else sorted({clip[0] for clip in CLIPS.values()}):
            response = client.get(f"{RAW}/assets/whale/{name}.png")
            response.raise_for_status()
            path = scratch / f"{name}.png"
            path.write_bytes(response.content)
            with Image.open(path) as source:
                if source.width != 2048 or source.height not in {2048, 4096}:
                    raise ValueError("Upstream atlas layout changed")
                atlases[name] = source.convert("RGBA")
        licenses = ROOT / "third_party/whale-avatar"
        licenses.mkdir(parents=True, exist_ok=True)
        for name in ("LICENSE", "ASSET_LICENSE.md"):
            response = client.get(f"{RAW}/{name}")
            response.raise_for_status()
            (licenses / name).write_bytes(response.content)

    manifest_path = ROOT / "assets/whale/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, (atlas, indices, durations) in [] if singing_only else CLIPS.items():
        write_clip(atlases[atlas], indices, durations, ROOT / f"assets/whale/{name}.gif", 384)
        entry = next(item for item in manifest["entries"] if item["id"] == name)
        entry.update(
            source=f"{REPOSITORY}/blob/{REVISION}/assets/whale/{atlas}.png",
            license_status="upstream_attribution_with_third_party_references",
            artwork_version="q-fullbody-1",
            source_frames=indices,
            duration_ms=durations,
        )
    for name, index in (("sing_open", 12), ("sing_closed", 13)):
        x, y = index % 4 * 512, index // 4 * 512
        tile = atlases["expressions"].crop((x, y, x + 512, y + 512))
        tile.resize((384, 384), Image.Resampling.LANCZOS).save(ROOT / f"assets/whale/{name}.png")
        manifest["entries"] = [entry for entry in manifest["entries"] if entry["id"] != name]
        manifest["entries"].append(
            {
                "id": name,
                "tag": name,
                "character_id": "whale",
                "path": f"assets/whale/{name}.png",
                "source": f"{REPOSITORY}/blob/{REVISION}/assets/whale/expressions.png",
                "source_frames": [index],
                "artwork_version": "q-fullbody-1",
                "license_status": "upstream_attribution_with_third_party_references",
            }
        )
    manifest["artwork_version"] = "q-fullbody-1"
    manifest["attribution"]["images"] = {
        "author": "YunYueSama",
        "repository": REPOSITORY,
        "revision": REVISION,
        "license": "大肥鱼项目署名许可 1.0",
        "license_path": "third_party/whale-avatar/LICENSE",
        "asset_license_path": "third_party/whale-avatar/ASSET_LICENSE.md",
        "design_sources": [
            "上善无形 https://space.bilibili.com/4456176",
            "ZipZipPipe https://space.bilibili.com/4168597",
        ],
        "note": (
            "Modified by Tuber Studio: extracted complete-character atlas frames, resized to 384px, "
            "and encoded six transparent GIF states. Talking is a greeting gesture, not lip sync. "
            "Upstream author licenses their own contributions; underlying community references retain their rights."
        ),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Updated singing PNG frames" if singing_only else "Updated six GIFs and singing PNG frames")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--singing-only", action="store_true")
    modes.add_argument("--reactions-only", action="store_true")
    options = parser.parse_args()
    if options.reactions_only:
        build_reactions()
    else:
        build(options.singing_only)
