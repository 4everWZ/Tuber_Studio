"""Join WHEP frames to sender RTP origins and attribute receiver observations.

oc_capture rows in whep-diagnostics.json identify construction, receipt of tracks,
element attachment and the first observed presentation. The final new_peer_play_ms
is the display timestamp minus the click, not the element-state poll from acquisition.
stats-owners.json parallels the raw stats rows; a null owner means not attributable.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path


def delta(timestamp, origin):
    return ((timestamp - origin + 2**31) % 2**32) - 2**31


def recovery(cut, stop, frames, events):
    clicked = cut["now"]
    previous = next((row[1] for row in reversed(frames) if row[0] <= clicked), cut["segment"])
    source = cut["segment"] if "binding" in cut else previous
    candidates = [row for row in frames if clicked < row[0] < stop and row[1] > source]
    if events is None:
        first = candidates[0] if candidates else None
        return {
            "new_peer_play_ms": first[0] - clicked if first else None,
            "presented_segment": first[1] if first else None,
            "peer_stages": None,
        }

    presented = {(row[1], row[0]) for row in candidates}
    attempts, attached, matches = {}, {}, []
    for event in sorted(events, key=lambda item: item["ms"]):
        segment = event["segment"]
        elapsed = event["ms"] - clicked
        if not (0 <= elapsed < stop - clicked and segment > source):
            continue
        peer = attempts.setdefault(segment, {"segment": segment, "created_ms": None, "attachments": []})
        if event["stage"] == "peer_created":
            peer["created_ms"] = elapsed
        elif event["stage"] == "video_attached":
            binding = {
                "binding": event["binding"],
                "track": event["track"],
                "attached_ms": elapsed,
                "first_presented_ms": None,
            }
            peer["attachments"].append(binding)
            attached[segment, event["binding"], event["track"]] = binding
        elif event["stage"] == "first_presented":
            binding = attached.get((segment, event["binding"], event["track"]))
            stamp = event["displayMs"]
            if binding is not None and (segment, stamp) in presented:
                binding["first_presented_ms"] = stamp - clicked
                matches.append((stamp - clicked, segment))
    delay, segment = min(matches) if matches else (None, None)
    return {"new_peer_play_ms": delay, "presented_segment": segment, "peer_stages": list(attempts.values())}


def stats_owners(stats, frames, pinned):
    owners = defaultdict(set)
    for _, segment, _, video, _, audio, _ in frames:
        for kind, source in (("video", video), ("audio", audio)):
            if source is not None:
                owners[kind, source].add(segment)
    rows = []
    for sample in stats:
        label = sample["segment"]
        matches = [
            owners[key]
            for kind in ("video", "audio")
            if (key := (kind, sample.get(kind, {}).get("ssrc"))) in owners
        ]
        common = set.intersection(*matches) if matches else set()
        if pinned:
            segment = label if not matches or label in common else None
        else:
            segment = next(iter(common)) if len(common) == 1 else None
        rows.append({"ms": sample["ms"], "polled_segment": label, "segment": segment})
    return rows


def review(folder, origins, capture_drops=None):
    metrics = json.loads((folder / "metrics.json").read_text())
    frames = json.loads((folder / "frames.json").read_text())
    levels = json.loads((folder / "levels.json").read_text())
    stats = json.loads((folder / "stats.json").read_text())
    ownership = stats_owners(stats, frames, bool(levels) and all("binding" in level for level in levels))
    (folder / "stats-owners.json").write_text(json.dumps(ownership, separators=(",", ":")))
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
    path = folder / "whep-diagnostics.json"
    diagnostics = json.loads(path.read_text()) if path.exists() else []
    events = [row for row in diagnostics if row.get("action") == "oc_capture"]
    cuts = json.loads((folder / "interrupts.json").read_text())
    stops = [cut["now"] for cut in cuts[1:]] + [metrics["end_browser_ms"]] if cuts else []
    for cut, check, stop in zip(cuts, metrics["interrupts"], stops, strict=True):
        check.update(recovery(cut, stop, frames, events if events or "binding" in cut else None))
        after = [level for level in levels if cut["now"] < level["now"] < stop]
        near = [level for level in after if 300 <= level["now"] - cut["now"] <= 410]
        check["before_rms"] = cut["rms"]

        def quiet(level):
            return not level.get("elementPlaying", level["playing"]) or (
                level.get("rmsAvailable", True) and level["rms"] < 0.004
            )

        check["quiet_at_350_ms"] = bool(near) and all(map(quiet, near))
        check["silent_ms"] = next((level["now"] - cut["now"] for level in after if quiet(level)), None)
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
