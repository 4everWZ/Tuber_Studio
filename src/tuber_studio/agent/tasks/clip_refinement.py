"""Suggest fresh subclip reads from contiguous positive observations in a rejected physical read."""

from itertools import groupby

from .delivery import NON_NARRATIVE, PROGRAMME_CONTEXT, WINDOW_REVIEW_VERSION


def subwindow_brief(proposals):
    return {"proposals": {key: {k: v for k, v in p.items() if k != "parent_objections"} for key, p in proposals.items()},
            "parent_objections": {p["parent_read"]: p["parent_objections"] for p in proposals.values()}}


def positive_subwindows(evidence, reviews):
    proposals = {}
    for read, review in reviews.items():
        clip = evidence.video_coverage.get(read, {}).get("clip")
        coverage = review.get("coverage", {})
        observations = review.get("observations") or []
        if (not clip or review.get("clip") != clip or review.get("screening_version") != WINDOW_REVIEW_VERSION
                or review.get("accepted") is not False or not coverage.get("complete") or coverage.get("uncovered") != []
                or coverage.get("scanned_frames") != clip["frames"] or not observations
                or [(o["frame"], o["seconds"]) for o in observations] !=
                   [(s["frame"], s["seconds"]) for s in coverage.get("samples", ())]):
            continue
        excluded_lines = {t["text"]: t.get("topic") for t in review.get("caption_topics", ()) if t.get("topic") not in NON_NARRATIVE
                          or t.get("subject") != PROGRAMME_CONTEXT or t.get("story_fact") is not None}

        def positive(o):
            line = o["screen_text"].strip()
            return (o["scene"] == "符合要求" and o["captions"] in {"无烧录字幕", "已读且符合"} and
                    (o["captions"] != "已读且符合" or bool(line)) and line not in excluded_lines)

        groups = [(good, list(rows)) for good, rows in groupby(observations, positive)]
        objections = [{"from": rows[0]["seconds"], "through": rows[-1]["seconds"],
                       "scene": rows[0]["scene"], "captions": rows[0]["captions"],
                       "basis": rows[0].get("basis"), "screen_text": rows[0]["screen_text"],
                       "rejected_topic": excluded_lines.get(rows[0]["screen_text"].strip())}
                      for good, rows in groups if not good]
        if not objections:
            continue
        for good, rows in groups:
            first, last = rows[0], rows[-1]
            if not good or last["frame"] - first["frame"] < 2:
                continue
            bounds = clip["start"], first["seconds"], last["seconds"], clip["end"]
            if not bounds[0] <= bounds[1] < bounds[2] < bounds[3]:
                continue
            window = {"bvid": clip["bvid"], "part": clip["part"], "start": bounds[1], "end": bounds[2]}
            proposals[f"{read}#{first['frame']}-{last['frame']}"] = {
                "parent_read": read, "cid": clip["cid"], "parent_interval": [bounds[0], bounds[3]],
                "window": window, "positive_frame_bounds": [first["frame"], last["frame"]],
                "observations": [{"seconds": o["seconds"], "screen_text": o["screen_text"], "basis": o.get("basis")}
                                 for o in (first, last)], "parent_objections": objections}
    return proposals
