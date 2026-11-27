"""Audio envelope extraction, multi-SFX/BGM kit synthesis, contact sheets, and optional VLM visual QC."""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import json
import math
import random
import re
import shutil
import struct
import wave
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ...core.identity import without_model_identity
from ...core.turns import operation_ticket
from ...core.validation import linked
from . import remotion_assets as _ra

_SFX_NAMES = ("transition", "whoosh", "pop", "chime", "chapter_hit")
MAX_RENDERED_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DURATION_DRIFT_SECONDS = 0.5


def _decode_pcm_samples(pcm: bytes, sampwidth: int) -> list[float]:
    if not pcm:
        return []
    if sampwidth == 2:
        count = len(pcm) // 2
        if count <= 0:
            return []
        vals = struct.unpack(f"<{count}h", pcm[: count * 2])
        return [v / 32768.0 for v in vals]
    if sampwidth == 1:
        return [(b - 128) / 128.0 for b in pcm]
    if sampwidth == 4:
        count = len(pcm) // 4
        if count <= 0:
            return []
        vals = struct.unpack(f"<{count}i", pcm[: count * 4])
        return [v / 2147483648.0 for v in vals]
    return []


def extract_audio_envelope(wav_path: Path | str, *, fps: int, total_frames: int) -> list[float]:
    """Compute per-frame normalized speech RMS [0.0 .. 1.0] with temporal smoothing."""
    n_frames = max(1, int(total_frames))
    fps_safe = max(1, int(fps))
    path = Path(wav_path)
    if not path.is_file():
        return [0.0] * n_frames

    try:
        with wave.open(str(path), "rb") as wf:
            channels = max(1, wf.getnchannels())
            sampwidth = wf.getsampwidth()
            framerate = max(1, wf.getframerate())
            pcm = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError, OSError):
        return [0.0] * n_frames

    samples = _decode_pcm_samples(pcm, sampwidth)
    if not samples:
        return [0.0] * n_frames
    if channels > 1:
        mono = [sum(samples[i : i + channels]) / channels for i in range(0, len(samples) - channels + 1, channels)]
    else:
        mono = samples

    samples_per_frame = framerate / float(fps_safe)
    raw_rms: list[float] = []
    total_samples = len(mono)
    for f_idx in range(n_frames):
        start = int(f_idx * samples_per_frame)
        end = max(start + 1, int((f_idx + 1) * samples_per_frame))
        if start >= total_samples:
            raw_rms.append(0.0)
            continue
        seg = mono[start : min(total_samples, end)]
        rms = math.sqrt(sum(s * s for s in seg) / max(1, len(seg)))
        raw_rms.append(rms)

    non_zero = sorted(v for v in raw_rms if v > 1e-5)
    if not non_zero:
        return [0.0] * n_frames
    ref_peak = non_zero[min(len(non_zero) - 1, int(len(non_zero) * 0.95))]
    ref_peak = max(ref_peak, 1e-4)

    normed = [min(1.0, max(0.0, v / ref_peak)) for v in raw_rms]
    smoothed: list[float] = []
    prev = 0.0
    for val in normed:
        # Fast attack, gentle release for smooth avatar ring & BGM ducking.
        alpha = 0.55 if val >= prev else 0.25
        prev = prev + alpha * (val - prev)
        smoothed.append(round(min(1.0, max(0.0, prev)), 4))
    return smoothed


def _write_mono_wav(path: Path, samples: Sequence[int], *, sample_rate: int) -> None:
    buf = struct.pack(f"<{len(samples)}h", *(max(-32767, min(32767, int(s))) for s in samples))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(buf)


def _synthesize_lofi_bgm(path: Path, *, duration_seconds: float, sample_rate: int) -> None:
    total_sec = max(4.0, float(duration_seconds) + 0.5)
    total_samples = int(round(total_sec * sample_rate))
    chords = (
        (130.81, 261.63, 329.63, 392.00, 493.88),  # Cmaj7 + bass
        (110.00, 220.00, 261.63, 329.63, 392.00),  # Am7 + bass
        (87.31, 174.61, 220.00, 261.63, 329.63),   # Fmaj7 + bass
        (98.00, 196.00, 246.94, 293.66, 329.63),   # G6 + bass
    )
    keys_notes = (523.25, 659.25, 587.33, 493.88, 440.00, 523.25, 392.00, 440.00)
    beat_sec = 0.5
    beat_samples = max(1, int(beat_sec * sample_rate))
    fade_in = int(0.8 * sample_rate)
    fade_out = int(1.2 * sample_rate)

    rng = random.Random(42)
    samples = [0] * total_samples
    for i in range(total_samples):
        t = i / sample_rate
        chord = chords[int(t // 4.0) % len(chords)]
        bass = math.sin(2.0 * math.pi * chord[0] * t) * 0.32
        pad = sum(math.sin(2.0 * math.pi * f * t) for f in chord[1:]) * 0.18
        beat_idx = i // beat_samples
        beat_pos = (i % beat_samples) / sample_rate
        note_f = keys_notes[beat_idx % len(keys_notes)]
        pluck_env = math.exp(-beat_pos * 4.5) * (1.0 - math.exp(-beat_pos * 85.0))
        pluck = (
            math.sin(2.0 * math.pi * note_f * beat_pos)
            + 0.20 * math.sin(4.0 * math.pi * note_f * beat_pos)
        ) * pluck_env * 0.36
        air = (rng.random() - 0.5) * 0.025
        env = 1.0
        if i < fade_in:
            env *= i / max(1, fade_in)
        if i > total_samples - fade_out:
            env *= max(0.0, (total_samples - i) / max(1, fade_out))
        samples[i] = int((bass + pad + pluck + air) * 1400.0 * env)
    _write_mono_wav(path, samples, sample_rate=sample_rate)


def _synthesize_sfx_kit(audio_dir: Path, *, sample_rate: int) -> dict[str, str]:
    # 1. transition.wav: warm two-tone marimba ding
    n_trans = int(0.22 * sample_rate)
    _write_mono_wav(
        audio_dir / "transition.wav",
        [
            int(math.sin(2.0 * math.pi * (783.99 if i / sample_rate < 0.08 else 1046.50) * (i / sample_rate))
                * 4000.0 * math.exp(-(i / sample_rate if i / sample_rate < 0.08 else i / sample_rate - 0.08) * 20.0)
                * (1.0 - math.exp(-max(0.0, i / sample_rate) * 160.0)))
            for i in range(n_trans)
        ],
        sample_rate=sample_rate,
    )
    # 2. whoosh.wav: airy swept breath
    rng = random.Random(7)
    n_whoosh = int(0.26 * sample_rate)
    whoosh = []
    lp = 0.0
    for i in range(n_whoosh):
        t = i / sample_rate
        env = math.sin(math.pi * min(1.0, t / 0.26)) ** 1.4
        noise = rng.random() * 2.0 - 1.0
        lp = lp * 0.72 + noise * 0.28
        sweep = math.sin(2.0 * math.pi * (520.0 - 340.0 * (t / 0.26)) * t) * 0.25
        whoosh.append(int((lp * 0.75 + sweep) * 2800.0 * env))
    _write_mono_wav(audio_dir / "whoosh.wav", whoosh, sample_rate=sample_rate)

    # 3. pop.wav: crisp bubble pop
    n_pop = int(0.10 * sample_rate)
    _write_mono_wav(
        audio_dir / "pop.wav",
        [
            int(math.sin(2.0 * math.pi * (960.0 - 540.0 * (i / n_pop)) * (i / sample_rate))
                * 4600.0 * math.exp(-(i / sample_rate) * 38.0))
            for i in range(n_pop)
        ],
        sample_rate=sample_rate,
    )

    # 4. chime.wav: shimmering harmonic chord
    n_chime = int(0.35 * sample_rate)
    _write_mono_wav(
        audio_dir / "chime.wav",
        [
            int((math.sin(2.0 * math.pi * 880.0 * (i / sample_rate))
                 + 0.65 * math.sin(2.0 * math.pi * 1318.5 * (i / sample_rate)))
                * 2600.0 * math.exp(-(i / sample_rate) * 10.5) * (1.0 - math.exp(-(i / sample_rate) * 140.0)))
            for i in range(n_chime)
        ],
        sample_rate=sample_rate,
    )

    # 5. chapter_hit.wav: deep sub thump + crisp bell
    n_hit = int(0.42 * sample_rate)
    _write_mono_wav(
        audio_dir / "chapter_hit.wav",
        [
            int((math.sin(2.0 * math.pi * (110.0 * math.exp(-(i / sample_rate) * 5.0) + 52.0) * (i / sample_rate)) * 0.75
                 + math.sin(2.0 * math.pi * 1046.5 * (i / sample_rate)) * 0.35 * math.exp(-(i / sample_rate) * 9.0))
                * 4400.0 * math.exp(-(i / sample_rate) * 7.0))
            for i in range(n_hit)
        ],
        sample_rate=sample_rate,
    )
    return {name: f"audio/{name}.wav" for name in _SFX_NAMES}


def prepare_bgm_and_sfx_kit(
    assets_root: Path | str,
    audio_dir: Path | str,
    *,
    duration_seconds: float,
    sample_rate: int = 24000,
) -> tuple[str, dict[str, str]]:
    """Use existing `assets/bgm/*.wav` or synthesize warm Lo-fi BGM + full SFX kit."""
    target_dir = Path(audio_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    bgm_dst = target_dir / "bgm.wav"

    copied_bgm = False
    bgm_src_dir = Path(assets_root) / "assets/bgm"
    if bgm_src_dir.is_dir() and not linked(bgm_src_dir):
        for cand in sorted(bgm_src_dir.glob("*.wav")):
            if linked(cand) or not cand.is_file():
                continue
            try:
                with wave.open(str(cand), "rb") as wf:
                    if wf.getnframes() > 0:
                        shutil.copy2(cand, bgm_dst)
                        copied_bgm = True
                        break
            except (wave.Error, OSError):
                continue

    if not copied_bgm:
        _synthesize_lofi_bgm(bgm_dst, duration_seconds=duration_seconds, sample_rate=sample_rate)

    sfx_map = _synthesize_sfx_kit(target_dir, sample_rate=sample_rate)
    return "audio/bgm.wav", sfx_map


async def _invoke_exec(run_exec, cmd: list[str], *, cwd: Path, timeout: float) -> tuple[int, str, str]:
    if callable(run_exec):
        res = run_exec(cmd, cwd=cwd, timeout=timeout)
        if inspect.isawaitable(res):
            res = await res
        return res
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def probe_rendered_video(output_path, *, expected_duration, dry_run, ffprobe_runner=None, run_exec=None):
    if callable(ffprobe_runner):
        probe = ffprobe_runner(output_path, expected_duration)
        return dict(await probe if inspect.isawaitable(probe) else probe)
    if dry_run:
        return {
            "has_video": True, "has_audio": True, "silent_audio": False,
            "duration_seconds": expected_duration, "width": 1920, "height": 1080, "dry_run": True,
        }
    if not (ffprobe := shutil.which("ffprobe")):
        raise ValueError("未安装 ffprobe，无法执行成片质检")
    code, stdout, stderr = await _invoke_exec(
        run_exec, [ffprobe, "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(output_path)],
        cwd=output_path.parent, timeout=30.0,
    )
    if code != 0:
        raise ValueError(f"ffprobe 质检失败: {stderr[:200]}")
    data = json.loads(stdout or "{}")
    streams, fmt = data.get("streams") or [], data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video_fields = video or {}
    probe = {
        "has_video": video is not None,
        "has_audio": audio is not None,
        "silent_audio": False,
        "duration_seconds": float(fmt.get("duration") or video_fields.get("duration") or 0.0),
        "width": int(video_fields.get("width") or 0),
        "height": int(video_fields.get("height") or 0),
    }
    if (ffmpeg := shutil.which("ffmpeg")) and audio is not None:
        fcode, _, ferr = await _invoke_exec(
            run_exec, [ffmpeg, "-hide_banner", "-i", str(output_path), "-af", "volumedetect", "-f", "null", "-"],
            cwd=output_path.parent, timeout=30.0,
        )
        if fcode == 0 and (match := re.search(r"mean_volume:\s*([-\d.]+)\s*dB", ferr)):
            mean_volume = float(match.group(1))
            probe |= {"mean_volume_db": mean_volume, "silent_audio": mean_volume <= -60.0}
    return probe


def validate_qc_probe(probe, output_path, expected_duration):
    size = output_path.stat().st_size if output_path.is_file() else 0
    probed_duration = float(probe.get("duration_seconds") or 0.0)
    drift, mean_db = abs(probed_duration - float(expected_duration)), probe.get("mean_volume_db")
    issues = [msg for condition, msg in (
        (size <= 0 or size > MAX_RENDERED_VIDEO_BYTES, "成片文件大小无效或超出平台限制"),
        (not probe.get("has_video"), "成片缺少有效视频轨"),
        (not probe.get("has_audio"), "成片缺少音频轨"),
        (probe.get("silent_audio") or (mean_db is not None and float(mean_db) <= -60.0), "成片音轨为全静音"),
        (drift > MAX_DURATION_DRIFT_SECONDS, f"音画时长偏差 {drift:.2f}s 超过 0.5s 上限"),
    ) if condition]
    return {
        "passed": not issues, "issues": issues, "size_bytes": size, "expected_duration_seconds": expected_duration,
        "probed_duration_seconds": probed_duration, "duration_drift_seconds": round(drift, 4),
        "width": int(probe.get("width") or 0), "height": int(probe.get("height") or 0),
    }


def _load_contact_sheet_font(size: int = 18):
    """Resolve a CJK-capable TrueType font on Windows/Linux before falling back to Pillow default."""
    candidates = (
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for font_path in candidates:
        if Path(font_path).is_file():
            try:
                return ImageFont.truetype(font_path, size=size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


async def extract_scene_contact_sheet(
    output_path: Path | str,
    scenes: Sequence[dict[str, Any]],
    *,
    fps: int,
    project_dir: Path | str,
    run_exec=None,
    ffmpeg_bin: str | None = None,
) -> tuple[Path | None, list[dict[str, Any]]]:
    """Grab 1 frame per scene (~45%) via ffmpeg and compose a labeled grid contact_sheet.png."""
    if not scenes or not output_path:
        return None, []
    proj_dir = Path(project_dir)
    out_file = Path(output_path)
    if linked(proj_dir) or linked(out_file):
        return None, []
    exe = ffmpeg_bin or shutil.which("ffmpeg")
    if run_exec is None and (not exe or not out_file.is_file()):
        return None, []
    exe = exe or "ffmpeg"

    fps_safe = max(1, int(fps))
    frames_dir = proj_dir / "contact_sheet"
    frames_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[dict[str, Any]] = []

    for idx, scene in enumerate(list(scenes)[:24], start=1):
        start_f = int(scene.get("start_frame") or 0)
        dur_f = max(1, int(scene.get("duration_frames") or fps_safe))
        ts = max(0.0, (start_f + dur_f * 0.45) / float(fps_safe))
        frame_path = frames_dir / f"scene_{idx:02d}.png"
        try:
            code, _out, _err = await _invoke_exec(
                run_exec,
                [exe, "-y", "-hide_banner", "-ss", f"{ts:.3f}", "-i", str(out_file), "-frames:v", "1", str(frame_path)],
                cwd=proj_dir,
                timeout=30.0,
            )
        except Exception:
            code = -1
        if code == 0 and frame_path.is_file() and not linked(frame_path):
            extracted.append({
                "scene_index": idx,
                "scene_id": str(scene.get("scene_id") or f"scene_{idx}"),
                "template": str(scene.get("template") or ""),
                "heading": str(scene.get("heading") or "")[:40],
                "timestamp_seconds": round(ts, 3),
                "path": str(frame_path),
                "rel_path": f"contact_sheet/scene_{idx:02d}.png",
            })

    if not extracted:
        return None, []

    cell_w, cell_h, label_h, pad = 480, 270, 38, 12
    cols = min(3, len(extracted))
    rows = math.ceil(len(extracted) / cols)
    sheet = Image.new("RGB", (cols * (cell_w + pad) + pad, rows * (cell_h + label_h + pad) + pad), (15, 23, 42))
    draw = ImageDraw.Draw(sheet)
    font = _load_contact_sheet_font(18)

    for pos, item in enumerate(extracted):
        r, c = divmod(pos, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (cell_h + label_h + pad)
        try:
            with Image.open(item["path"]) as im:
                thumb = im.convert("RGB").resize((cell_w, cell_h), Image.Resampling.LANCZOS)
                sheet.paste(thumb, (x, y))
        except Exception:
            draw.rectangle((x, y, x + cell_w, y + cell_h), fill=(30, 41, 59))
        caption = f"#{item['scene_index']:02d} {item['template']} | {item['heading'][:22]}"
        draw.text((x + 8, y + cell_h + 9), caption, fill=(226, 232, 240), font=font)

    sheet_path = proj_dir / "contact_sheet.png"
    sheet.save(sheet_path, format="PNG")
    return sheet_path, extracted


def _static_monotony_assessment(plan_dict: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    scenes = plan_dict.get("scenes") if isinstance(plan_dict, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return "unknown", [], []
    templates = [str(s.get("template") or "") for s in scenes if isinstance(s, dict)]
    themes = [str(s.get("theme") or "") for s in scenes if isinstance(s, dict) and s.get("theme")]
    issues: list[str] = []
    suggestions: list[str] = []
    adjacent_dups = sum(1 for a, b in zip(templates, templates[1:]) if a and a == b)
    if adjacent_dups:
        issues.append(f"存在 {adjacent_dups} 处相邻分镜模板重复")
        suggestions.append("将相邻重复模板替换为 ChapterBumper / KineticStatement / MemeCutaway 以增强视觉节奏")
    if len(scenes) >= 6 and len(set(themes)) <= 1:
        suggestions.append("建议在章节间交替使用 dark_tech / aqua_light / cinema 主题背景")
    risk = "high" if adjacent_dups >= 2 else ("medium" if adjacent_dups == 1 or len(set(templates)) <= 2 else "low")
    return risk, issues, suggestions


async def run_vlm_visual_review(
    runtime,
    contact_sheet_path: Path | str | None,
    scene_frames: Sequence[dict[str, Any]],
    plan_dict: dict[str, Any],
) -> dict[str, Any]:
    """Run optional non-blocking VLM visual review on contact_sheet.png; degrades cleanly when unavailable."""
    risk, static_issues, static_suggestions = _static_monotony_assessment(plan_dict)
    sheet_file = Path(contact_sheet_path) if contact_sheet_path else None
    if sheet_file is None or not sheet_file.is_file() or linked(sheet_file):
        return {
            "ok": True,
            "reviewed": False,
            "overall_score": 0,
            "monotony_risk": risk,
            "layout_issues": static_issues or ["未找到可供审阅的分镜缩略图 contact_sheet.png"],
            "suggestions": static_suggestions,
            "scenes": [],
        }

    provider = getattr(runtime, "vision_llm", None)
    generate_fn = getattr(provider, "generate_json", None) if provider is not None else None
    if not callable(generate_fn):
        vp = getattr(runtime, "visual_provider", None)
        api = getattr(vp, "api", None) if vp is not None else None
        generate_fn = getattr(vp, "generate_json", None) or getattr(api, "generate_json", None)
    if not callable(generate_fn):
        op_llm = getattr(runtime, "operation_llm", None)
        if op_llm is not None and callable(getattr(op_llm, "generate_json", None)):
            settings = getattr(runtime, "settings", None)
            has_key = callable(getattr(settings, "provider_key", None)) and bool(settings.provider_key("omni"))
            is_real_api = type(op_llm).__name__ == "APIProvider"
            if has_key or not is_real_api:
                generate_fn = op_llm.generate_json

    if not callable(generate_fn):
        return {
            "ok": True,
            "reviewed": False,
            "overall_score": 0,
            "monotony_risk": risk,
            "layout_issues": static_issues,
            "suggestions": static_suggestions or ["当前未配置可用视觉模型，已跳过 VLM 画面评审。"],
            "scenes": [],
        }

    try:
        with Image.open(sheet_file) as img:
            rgb = img.convert("RGB")
            if rgb.width > 1440:
                new_h = max(1, int(round(rgb.height * (1440 / rgb.width))))
                rgb = rgb.resize((1440, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="PNG")
            img_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        return {
            "ok": True,
            "reviewed": False,
            "overall_score": 0,
            "monotony_risk": risk,
            "layout_issues": [*static_issues, f"读取缩略图失败: {_ra._sanitize_diagnostic(str(exc))}"],
            "suggestions": static_suggestions,
            "scenes": [],
        }

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["overall_score", "monotony_risk", "layout_issues", "suggestions", "scenes"],
        "properties": {
            "overall_score": {"type": "integer", "minimum": 1, "maximum": 100},
            "monotony_risk": {"type": "string", "enum": ["low", "medium", "high"]},
            "layout_issues": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}},
            "suggestions": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 200}},
            "scenes": {
                "type": "array",
                "maxItems": 24,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["scene_index", "verdict", "note"],
                    "properties": {
                        "scene_index": {"type": "integer", "minimum": 1},
                        "verdict": {"type": "string", "enum": ["pass", "polish", "fix"]},
                        "note": {"type": "string", "maxLength": 200},
                    },
                },
            },
        },
    }
    scene_meta = [
        {"scene_index": s.get("scene_index"), "template": s.get("template"), "heading": s.get("heading")}
        for s in scene_frames
    ]
    messages = [
        {
            "role": "system",
            "content": "你是严谨的视频视觉导演与版式评审。检查接触表（contact_sheet）中的分镜排版、文字可读性、字幕遮挡与镜头多样性，返回 JSON。",
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"分镜清单：{scene_meta}。请从版式层次、留白、元素重叠与节奏单调风险给出评审。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
        },
    ]

    slot = (
        nullcontext()
        if operation_ticket.get() is not None or not callable(getattr(runtime, "offline_generation", None))
        else runtime.offline_generation("background")
    )
    try:
        async with asyncio.timeout(45.0), slot:
            with without_model_identity():
                raw = await generate_fn(messages, schema, max_tokens=1024)
        if not isinstance(raw, dict):
            raise ValueError("视觉评审未返回 JSON 对象")
        return {
            "ok": True,
            "reviewed": True,
            "overall_score": max(1, min(100, int(raw.get("overall_score") or 75))),
            "monotony_risk": str(raw.get("monotony_risk") or risk),
            "layout_issues": [str(x)[:200] for x in (raw.get("layout_issues") or ())][:10],
            "suggestions": [str(x)[:200] for x in (raw.get("suggestions") or ())][:10],
            "scenes": [dict(x) for x in (raw.get("scenes") or ()) if isinstance(x, dict)][:24],
        }
    except Exception as exc:
        return {
            "ok": True,
            "reviewed": False,
            "overall_score": 0,
            "monotony_risk": risk,
            "layout_issues": static_issues,
            "suggestions": [*static_suggestions, f"视觉模型评审未启用或失败: {_ra._sanitize_diagnostic(f'{type(exc).__name__}: {exc}')}"],
            "scenes": [],
        }
