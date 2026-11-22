"""Measure decoded WHEP mouth pixels against audio PTS in the same Chromium recording."""

import json
import sys

import av
import cv2
import numpy as np


def inspect(file, cx, cy, reference=None):
    cx, cy = int(cx), int(cy)
    times, power, audible = [], [], []
    with av.open(file) as recording:
        for frame in recording.decode(audio=0):
            samples = frame.to_ndarray().astype(float)
            if frame.format.is_planar:
                samples = samples.mean(axis=0)
            else:
                samples = samples.reshape(-1, len(frame.layout.channels)).mean(axis=1)
            if not frame.format.name.startswith(("flt", "dbl")):
                samples /= 2 ** (frame.format.bits - 1)
            times.append(frame.time + frame.samples / frame.sample_rate / 2)
            power.append(np.mean(samples**2))
            size = frame.sample_rate // 200
            for start in range(0, len(samples) - size + 1, size):
                if np.mean(samples[start : start + size] ** 2) > 0.012**2:
                    audible.append(frame.time + start / frame.sample_rate)
    if not times:
        raise ValueError("WHEP recording has no decoded audio")
    rows = []
    face_errors = []
    if reference:
        from PIL import Image, ImageOps

        with Image.open(reference) as source:
            ref = np.asarray(ImageOps.pad(source.convert("RGB"), (640, 720), method=Image.Resampling.LANCZOS))

        def face_sample(image):
            return cv2.resize(image[90:560, 80:560], (48, 47), interpolation=cv2.INTER_AREA).astype(float)

        expected = face_sample(ref)
    with av.open(file) as recording:
        for frame in recording.decode(video=0):
            rgb = frame.to_ndarray(format="rgb24")
            if rgb.shape[:2] != (720, 640):
                rgb = cv2.resize(rgb, (640, 720))
            if reference:
                face_errors.append(np.abs(face_sample(rgb) - expected).mean())
            red, green, _ = rgb[cy - 42 : cy + 43, cx - 49 : cx + 50].astype(np.int16).transpose(2, 0, 1)
            mask = ((red - green > 50) & (green < 207) & (red > 136)).astype(np.uint8)
            _, _, stats, centers = cv2.connectedComponentsWithStats(mask)
            area = max(
                (
                    int(stat[4])
                    for stat, (x, y) in zip(stats[1:], centers[1:])
                    if abs(x - 49) < 36 and abs(y - 42) < 32
                ),
                default=0,
            )
            rows.append((frame.time, area))
    if not rows:
        raise ValueError("WHEP recording has no decoded video")
    pts, areas = np.asarray(rows).T
    rms = np.sqrt(np.interp(pts, times, power))
    silence, voice = areas[rms < 0.004], areas[rms > 0.025]

    def median(values):
        return round(float(np.median(values)), 1) if len(values) else None

    first_sound = next(
        (first for first, second in zip(audible, audible[1:]) if abs(second - first - 0.005) < 0.0005), None
    )
    open_pts = pts[areas >= 120]
    return {
        "reference_face_mae": median(face_errors),
        "recorded_video_frames": len(rows),
        "recorded_span_seconds": round(float(pts[-1] - pts[0]), 3),
        "first_open_minus_voice_ms": round(1000 * (float(open_pts[0]) - first_sound))
        if len(open_pts) and first_sound is not None
        else None,
        "recorded_quiet_area_px": median(silence),
        "recorded_voiced_area_px": median(voice),
        "recorded_quiet_frames": len(silence),
        "recorded_voiced_frames": len(voice),
    }


if __name__ == "__main__":
    print(json.dumps(inspect(*sys.argv[1:])))
