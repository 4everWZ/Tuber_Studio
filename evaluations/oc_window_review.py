"""Join laptop WHEP presentation pairs to observed sender RTP origins, never to first receiver samples."""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def delta(timestamp, origin):
    return ((timestamp - origin + 2**31) % 2**32) - 2**31


def review(folder, origins, capture_drops=None):
    metrics = json.loads((folder / "metrics.json").read_text())
    frames = json.loads((folder / "frames.json").read_text())
    levels = json.loads((folder / "levels.json").read_text())
    stats = json.loads((folder / "stats.json").read_text())
    groups = defaultdict(list)
    for stamp, segment, _, video, video_stamp, audio, audio_stamp in frames:
        if None not in (video, video_stamp, audio, audio_stamp):
            groups[segment, video, audio].append((stamp, video_stamp, audio_stamp))
    pairs, lower, missing, counts = [], [], [], []
    for (segment, video, audio), rows in groups.items():
        local = [delta(v, rows[0][1]) / 90 - delta(a, rows[0][2]) / 48 for _, v, a in rows]
        lower.append((max(local) - min(local)) / 2)
        counts.append({"segment": segment, "pairs": len(rows)})
        if video not in origins or audio not in origins:
            missing.append(segment)
            continue
        for stamp, video_stamp, audio_stamp in rows:
            video_ms = delta(video_stamp, origins[video]) / 90
            audio_ms = delta(audio_stamp, origins[audio]) / 48
            if min(video_ms, audio_ms) < 0:
                raise ValueError("Capture started after RTP transmission; not a valid sender origin")
            pairs.append([stamp - metrics["start_browser_ms"], segment, round(video_ms - audio_ms, 3)])
    for cut, check in zip(
        json.loads((folder / "interrupts.json").read_text()), metrics["interrupts"], strict=True
    ):
        near = [level for level in levels if 300 <= level["now"] - cut["now"] <= 410]
        check["before_rms"] = cut["rms"]
        check["quiet_at_350_ms"] = bool(near) and all(
            not level["playing"] or level["rms"] < 0.004 for level in near
        )
    extremes = max((abs(row[2]) for row in pairs), default=None)
    over = sum(abs(row[2]) > 160 for row in pairs)
    endpoints = [metrics["start_browser_ms"], *[row[0] for row in frames], metrics["end_browser_ms"]]
    gap = max(right - left for left, right in zip(endpoints, endpoints[1:]))
    last = {s["video"]["ssrc"]: s["video"] for s in stats if "video" in s}
    metrics.update(
        sender_observer_socket_drops=capture_drops,
        anchored_pairs=len(pairs),
        pairs_by_generation=counts,
        missing_origin_segments=missing,
        max_absolute_rtp_ms=extremes if not missing else None,
        observed_max_ms=extremes,
        max_abs_lower_bound_without_origin_ms=max(lower, default=None),
        over_160_ms=over if not missing else None,
        presentation_gap_ms=round(gap, 1),
        video_lost=sum(v["lost"] for v in last.values()),
        observed_freeze_seconds=round(sum(v.get("freezeSeconds", 0) for v in last.values()), 3),
    )
    metrics["passed"] = bool(
        metrics["closed"]
        and metrics["source_bound"]
        and metrics["accepted_ack_ms"]
        and metrics["fps"] >= 25
        and metrics["freezes"] == 0
        and capture_drops == 0
        and extremes is not None
        and not missing
        and extremes <= 160
        and len(metrics["interrupts"]) >= 3
        and all(c["new_peer_play_ms"] is not None and c["quiet_at_350_ms"] for c in metrics["interrupts"])
        and (not metrics["mixed"] or metrics["response_readbacks"] >= metrics["seconds"] // 15)
    )
    metrics["admission"] = "qualified" if metrics["passed"] else "not_qualified"
    (folder / "relative-clock.json").write_text(json.dumps(pairs, separators=(",", ":")))
    (folder / "metrics.json").write_text(json.dumps(metrics, indent=2))
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origins", type=Path)
    parser.add_argument("folders", type=Path, nargs="+")
    args = parser.parse_args()
    origins = {}
    capture_drops = None
    for line in args.origins.read_text().splitlines():
        item = json.loads(line)
        if "ssrc" in item:
            origins.setdefault(item["ssrc"], item["timestamp"])
        if "drops" in item:
            capture_drops = item["drops"]
    for folder in args.folders:
        print(folder, review(folder, origins, capture_drops)["admission"])
