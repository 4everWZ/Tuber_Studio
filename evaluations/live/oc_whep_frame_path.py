"""Attribute native 25 FPS WHEP frame gaps using private sender headers and receiver traces."""
import argparse
import json
from collections import defaultdict
from math import ceil
from pathlib import Path

FRAME_TICKS = 3600  # 90 kHz at 25 FPS
MODULUS = 2**32


def percentile(values, p):
    return round(sorted(values)[ceil(len(values) * p) - 1], 2) if values else None


def source_clock(rows, origins, packets, markers):
    """Compare egress times at the same media offset used by the receiver gate."""
    matched, exceeded = [], []
    for _, _, _, video, stamp, audio, audio_stamp in rows:
        if video not in origins or audio not in origins or audio_stamp is None:
            continue
        ticks = (stamp - origins[video]) % MODULUS
        audio_offset, remainder = divmod(ticks * 48000, 90000)
        simultaneous = (origins[audio] + audio_offset) % MODULUS
        if remainder or simultaneous not in packets[audio] or stamp not in markers[video]:
            continue
        # Last original video fragment vs first original audio packet, in ms.
        skew = markers[video][stamp] - packets[audio][simultaneous]
        matched.append(abs(skew))
        receiver_skew = ticks / 90 - ((audio_stamp - origins[audio]) % MODULUS) / 48
        if abs(receiver_skew) > 160:
            exceeded.append(abs(skew))
    return {"pairs": len(matched), "max_ms": round(max(matched), 2) if matched else None,
            "over_gate_pairs": len(exceeded), "over_gate_max_ms": round(max(exceeded), 2) if exceeded else None}


def analyze(folder):
    def load(name):
        return json.loads((folder / f"{name}.json").read_text(encoding="utf8"))

    wire = load("wire")
    if wire["drops"]:
        raise ValueError("Incomplete sender capture")
    first_packets, markers = defaultdict(dict), defaultdict(dict)
    origins = {}
    for source, stamp, _, pt, utc_us in wire["packets"]:
        origins.setdefault(source, stamp)
        first_packets[source].setdefault(stamp, utc_us / 1000)
        if pt & 128:
            markers[source].setdefault(stamp, utc_us / 1000)

    segments = defaultdict(list)
    all_frames = load("frames")
    for frame in all_frames:
        segments[frame[1]].append(frame)
    # Match by both RTP and display time, including across subscription changes.
    metadata = {(row[0], row[4]): row for row in load("receive")["frames"]}
    stats = load("stats")
    result = []
    repair_delays, plain_arrivals, repaired_arrivals = [], [], []
    slots_near_repair = slots_plain = deficit_near_repair = deficit_plain = 0
    for segment, frames in segments.items():
        sources = {frame[3] for frame in frames if frame[3] is not None}
        if len(sources) != 1:
            raise ValueError("Expected exactly one primary video SSRC per segment")
        source = sources.pop()
        sent = markers[source]
        # RTX copies the original timestamp onto a separate SSRC. Require a
        # unique companion stream whose entire set overlaps the primary.
        candidates = [stream for other, stream in first_packets.items()
                      if other != source and len(stream) >= 2 and stream.keys() <= first_packets[source].keys()]
        if len(candidates) > 1:
            raise ValueError("Ambiguous repair stream")
        repair = candidates[0] if candidates else {}
        repair_delays.extend(repair[t] - sent[t] for t in repair.keys() & sent.keys())
        arrivals = []
        for frame in frames:
            ts = frame[4]
            m = metadata.get((ts, frame[0]))
            if m and m[1] is not None and ts in sent:
                arrivals.append((ts in repair, m[1] - sent[ts]))
        # Clocks have different origins. Compare excess delays within one
        # short subscription; this does not estimate absolute one-way latency.
        baseline = percentile([delay for retransmitted, delay in arrivals if not retransmitted], .10)
        if baseline is not None:
            for retransmitted, delay in arrivals:
                (repaired_arrivals if retransmitted else plain_arrivals).append(delay - baseline)

        for left, right in zip(frames, frames[1:]):
            ticks = (right[4] - left[4]) % MODULUS
            if ticks % FRAME_TICKS or ticks >= MODULUS // 2:
                raise ValueError("Unexpected frame clock or order")
            slots = ticks // FRAME_TICKS
            deficit = max(0, slots - (right[2] - left[2]))
            # Inclusive endpoints: this is an association with a display
            # interval, not a count of frames proven lost to retransmission.
            near_repair = any((left[4] + step) % MODULUS in repair for step in range(0, ticks + 1, FRAME_TICKS))
            if near_repair:
                slots_near_repair += slots
                deficit_near_repair += deficit
            else:
                slots_plain += slots
                deficit_plain += deficit

        by_count = {frame[2]: frame for frame in frames}
        same = [s for s in stats if s.get("video", {}).get("ssrc") == source and
                s["state"] == "connected" and s["presented"] in by_count]
        if len(same) < 2:
            result.append({"segment": segment, "matched_samples": len(same)})
            continue
        a, b = same[0], same[-1]
        first, last = (by_count[s["presented"]][4] for s in (a, b))
        ticks = (last - first) % MODULUS
        if first not in sent or last not in sent or not 0 < ticks < MODULUS // 2:
            raise ValueError("No sender marker for a presented endpoint")
        timeline = sorted(((t - first) % MODULUS, utc) for t, utc in sent.items() if (t - first) % MODULUS <= ticks)
        if any(t % FRAME_TICKS for t, _ in timeline):
            raise ValueError("Unexpected sender frame grid")
        gaps = [y[1] - x[1] for x, y in zip(timeline, timeline[1:])]
        result.append(dict(segment=segment, matched_samples=len(same), grid=ticks // FRAME_TICKS,
                           emitted=len(timeline) - 1, decoded=b["video"]["decoded"] - a["video"]["decoded"],
                           presented=b["presented"] - a["presented"], send_gap_max_ms=round(max(gaps), 2),
                           send_gap_p95_ms=percentile(gaps, .95)))

    rtt = [s["route"]["rtt"] * 1000 for s in stats if s.get("route", {}).get("rtt") is not None]
    return dict(segments=result, source_clock=source_clock(all_frames, origins, first_packets, markers),
                repair_stamps=len(repair_delays), repair_lag_p50_ms=percentile(repair_delays, .50),
                rtt_p50_ms=percentile(rtt, .50), plain_arrivals=len(plain_arrivals), repaired_arrivals=len(repaired_arrivals),
                plain_excess_p95_ms=percentile(plain_arrivals, .95), repaired_excess_p95_ms=percentile(repaired_arrivals, .95),
                slots_near_repair=slots_near_repair, slots_plain=slots_plain,
                deficit_near_repair=deficit_near_repair, deficit_plain=deficit_plain)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    print(json.dumps(analyze(parser.parse_args().folder), indent=2))
