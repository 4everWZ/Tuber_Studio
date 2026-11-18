"""Measure decoded WHEP mouth pixels against audio PTS in the same Chromium recording."""

import json
import sys

import av
import cv2
import numpy as np


def inspect(file, profile):
    cx, cy = (347, 465) if profile == "q" else (370, 418)
    times, power = [], []
    with av.open(file) as recording:
        for frame in recording.decode(audio=0):
            samples = frame.to_ndarray().astype(float)
            if frame.format.is_planar:
                samples = samples.mean(axis=0)
            else:
                samples = samples.ravel()
            if not frame.format.name.startswith(("flt", "dbl")):
                samples /= 2 ** (frame.format.bits - 1)
            times.append(frame.time + frame.samples / frame.sample_rate / 2)
            power.append(np.mean(samples**2))
    if not times:
        raise ValueError("WHEP recording has no decoded audio")
    rows = []
    with av.open(file) as recording:
        for frame in recording.decode(video=0):
            rgb = frame.to_ndarray(format="rgb24")
            if rgb.shape[:2] != (720, 640):
                rgb = cv2.resize(rgb, (640, 720))
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

    return {
        "recorded_quiet_area_px": median(silence),
        "recorded_voiced_area_px": median(voice),
        "recorded_quiet_frames": len(silence),
        "recorded_voiced_frames": len(voice),
    }


if __name__ == "__main__":
    print(json.dumps(inspect(*sys.argv[1:])))
