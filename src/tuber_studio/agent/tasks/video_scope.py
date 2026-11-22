"""Owner-selected video bounds, shared by admission, recovery and evidence reuse."""

import re
from urllib.parse import parse_qs, urlsplit

_BVID = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])")
_PART = re.compile(r"(?:[?&]p=|(?<![A-Za-z0-9])p\s*)([1-9]\d{0,8})(?!\d)|第\s*([1-9]\d{0,8})\s*[pP]", re.I)


def _parts(text):
    return {int(left or right): None for left, right in _PART.findall(text)}


def selection_plan(selection, goal):
    result = {"video_selection": selection.model_dump(mode="json")}
    latest = goal.rsplit("\n号主改口：", 1)[-1]
    if not selection.ranges and set(_BVID.findall(latest)) <= {selection.bvid}:
        if parts := _parts(latest):
            result["video_parts"] = sorted(parts)
    return result


def video_binding(task):
    selected = task["plan"].get("video_selection")
    if selected:
        parts = {}
        for interval in selected["ranges"]:
            bounds = parts.setdefault(interval["part"], [])
            if bounds and bounds[-1][1] == interval["start"]:
                bounds[-1] = (bounds[-1][0], interval["end"])
            else:
                bounds.append((interval["start"], interval["end"]))
        if parts:
            return {selected["bvid"]: parts}
        return {selected["bvid"]: dict.fromkeys(task["plan"].get("video_parts", ())) or None}

    bound = {}
    for goal in [*task["plan"]["prior_goals"], task["goal"]]:
        goal = goal.rsplit("\n号主改口：", 1)[-1]
        mentions = list(_BVID.finditer(goal))
        videos = {match.group() for match in mentions}
        parts = _parts(goal)
        if len(videos) == 1:
            video = next(iter(videos))
            bound = {video: parts or bound.get(video)}
        elif videos and not parts:
            bound = dict.fromkeys(videos)
        elif videos:
            bound = {}
            if _parts(goal[:mentions[0].start()]):
                continue
            for index, mention in enumerate(mentions):
                stop = mentions[index + 1].start() if index + 1 < len(mentions) else len(goal)
                suffix = goal[mention.end():stop]
                direct = re.match(r"\s*(?:的\s*|/\s*)?", suffix).end()
                assigned = {}
                while match := _PART.match(suffix, direct):
                    assigned[int(match[1] or match[2])] = None
                    direct = match.end()
                    direct += re.match(r"\s*(?:[、，,和及与]\s*)?", suffix[direct:]).end()
                if _parts(suffix[direct:]):
                    bound = {}
                    break
                if assigned:
                    bound.setdefault(mention.group(), {}).update(assigned)
        elif parts:
            bound = {next(iter(bound)): parts} if len(bound) == 1 else {}
    return bound


def permits(bound, arguments, *, point=False):
    video, part = arguments.get("bvid"), arguments.get("part")
    if video not in bound:
        return False
    parts = bound[video]
    if parts is None:
        return True
    if part not in parts:
        return False
    if parts[part] is None:
        return True
    if point:
        return any(left <= arguments["seconds"] < right for left, right in parts[part])
    start, end = arguments.get("start"), arguments.get("end")
    return (start is not None and end is not None and
            any(left <= start < end <= right for left, right in parts[part]))


def scoped_video_step(step, bound):
    arguments, result = step["arguments"], step.get("result")
    video = arguments["bvid"]
    if video not in bound:
        return None
    whole_request = permits(bound, arguments)
    if not result:
        return step if whole_request else None
    if result.get("selection", {}).get("bvid", video) != video:
        return None
    findings = []
    for item in result.get("findings", []):
        part, seconds = item.get("part"), item.get("seconds")
        url = urlsplit(item.get("url") or "")
        query = parse_qs(url.query)
        valid = (type(part) is int and type(seconds) in (int, float) and seconds >= 0
                 and (url.scheme, url.netloc, url.path.rstrip("/")) == ("https", "www.bilibili.com", f"/video/{video}")
                 and query.get("p", ["1"]) == [str(part)]
                 and ("t" not in query or query["t"] == [str(int(seconds))])
                 and (arguments.get("part") in (None, part))
                 and (arguments.get("start") is None or arguments["start"] <= seconds < arguments["end"])
                 and permits(bound, {"bvid": video, "part": part, "seconds": seconds}, point=True))
        if valid and item.get("source_kind") == "archived_subtitle":
            end = item.get("end")
            valid = (type(end) in (int, float) and seconds < end
                     and permits(bound, {"bvid":video, "part":part, "start":seconds, "end":end}))
        findings.append(item if valid else {})
    if not whole_request:
        if not any(findings):
            return None
        result = {key: result[key] for key in ("status", "viewing") if key in result}
    return step | {"result": result | {"findings": findings}}
