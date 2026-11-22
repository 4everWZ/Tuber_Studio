"""Private task delivery built only from persisted tool receipts."""

import json
import math
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, PrivateAttr, model_validator

from ...core.models import StrictModel
from ...infra.web_client.fetch import programme_credit_text

LABELS = {
    "publish_dynamic_batch": "B 站动态发布",
    "inbox_received": "定向来信入库回执",
    "web_search": "检索候选来源", "web_page": "读取网页", "bilibili_owner": "B 站号主",
    "operating_account": "B 站运营账号", "operation_status": "自主运营", "today_activity": "当日运营事件", "inspect_video": "视频取材",
    "live_status": "直播状态", "live_handoff": "节目交接", "game_status": "模拟游戏", "game_action": "模拟游戏动作",
}
OPERATIONS = {
    "off": "未启动", "starting": "正在启动", "running": "运行中", "blocked": "有阻塞",
    "emergency": "紧急停止", "stopping": "正在停止", "draining": "正在收尾",
}
PLATFORM = {"stopped": "已停止", "live": "直播中", "paused": "已暂停"}
PROGRAM = {"idle": "空闲", "starting": "正在启动", "publishing": "正在发布", "stopping": "正在停止"}
VIEWING = {"watched": "取得字幕及画面", "subtitles_only": "仅取得字幕", "visual_only": "仅取得画面", "partial": "部分取材"}
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()<>#!|~])")


def _text(value):
    return _MARKDOWN.sub(r"\\\1", " ".join(value.split())) if isinstance(value, str) else ""


def _source(value):
    if not isinstance(value, str) or re.search(r"[\s\\<>\x00-\x1f]", value):
        return ""
    try:
        url = urlsplit(value)
    except ValueError:
        return ""
    return f"<{value}>" if url.scheme == "https" and url.hostname and url.username is None else ""


def _materials(items):
    materials = []
    for item in items or []:
        if not isinstance(item, dict) or not (text := _text(item.get("text") or item.get("title"))):
            continue
        label = _text(item.get("kind"))
        if type(item.get("part")) is int and type(item.get("seconds")) in {int, float}:
            label = f"分 P {item['part']} · {item['seconds']} 秒" + (f" · {label}" if label else "")
        materials.append(" · ".join(part for part in (label, text, _source(item.get("url"))) if part))
    return materials


class CreativeItem(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=1200)
    sources: list[str] = Field(description="本项引用的证据 ID。指定主帧时可只列补充来源。")
    primary_frame: str | None = Field(default=None, description="本项主画面的单个证据 ID；纯文字项填 null。")

    @model_validator(mode="after")
    def include_primary_citation(self):
        if self.primary_frame is not None and self.primary_frame not in self.sources:
            self.sources = [self.primary_frame, *self.sources]
        if not self.sources:
            raise ValueError("交付项至少需要一个证据 ID。")
        return self


class PrivateDelivery(StrictModel):
    items: list[CreativeItem]
    gaps: list[str]
    table_columns: tuple[str, str] | None = Field(
        default=None, exclude_if=lambda value: value is None,
        description="文字交付要求两栏对照表时，填写两个栏名。每项 title 和 rationale 分别是同一行的两格。",
    )
    _field_sources: dict = PrivateAttr(default_factory=dict)
    _frame_reviews: dict = PrivateAttr(default_factory=dict)
    _window_reviews: dict = PrivateAttr(default_factory=dict)
    _locked_fields: set[str] = PrivateAttr(default_factory=set)
    _attribution_reviews: list[dict] = PrivateAttr(default_factory=list)


class DelegationError(ValueError):
    """A diagnostic safe to show without exposing provider payloads."""

    def __init__(self, message, *, attribution_reviews=()):
        super().__init__(message)
        self.attribution_reviews = list(attribution_reviews)


class AdditionalVideoRead(DelegationError):
    """The requested revision requires a new, authorized reading before editing."""

    def __init__(self, message, exclude=(), retained=(), *, sources=False, video=True, windows=(), lookup=None, reviews=None,
                 frames=None):
        super().__init__(message)
        self.exclude = list(exclude)
        self.retained = list(retained)
        self.sources = sources
        self.video = video
        self.windows = list(windows)
        self.lookup = lookup
        self.reviews = reviews
        self.frames = frames


class EditorialCorrection(DelegationError):
    """One private editorial draft and the specific fields Omni rejected."""

    def __init__(self, product, issues):
        super().__init__("前台要求按回执修正选材文字。", attribution_reviews=product._attribution_reviews)
        self.draft = product.model_dump()
        self.issues = issues
        self.frame_reviews = product._frame_reviews
        self.window_reviews = product._window_reviews
        self.locked_fields = sorted(product._locked_fields)


def editorial_fields(product):
    fields = {f"items.{i}.{name}": getattr(item, name) for i, item in enumerate(product.items)
              for name in ("title", "rationale")}
    fields.update({f"gaps.{i}": gap for i, gap in enumerate(product.gaps)})
    return fields


def retain_reviewed_items(product, kept, locked_fields):
    """Move the surviving items and their field evidence to the same new indices."""
    paths = {f"items.{i}.{field}": f"items.{j}.{field}" for j, (i, _) in enumerate(kept)
             for field in ("title", "rationale")}
    product.items = [item for _, item in kept]
    product._field_sources = {new: product._field_sources[old] for old, new in paths.items()
                              if old in product._field_sources}
    return {new for old, new in paths.items() if old in locked_fields}


def exhausted_video_gaps(product, evidence, rejected, approved, locked):
    """Only omit locally rejected video copy; keep diagnostics and rejected quotes out of the card."""
    if not all(primary_frame(item, evidence) for item in product.items):
        return {}
    for receipt in product._attribution_reviews:
        for claim in receipt["claims"]:
            if claim.get("issue") and (not claim["phrase"] or claim["phrase"] not in receipt["text"]
                    or (claim.get("person") or claim.get("target") or "") not in claim["phrase"]
                    or (claim.get("work_type") or "") not in claim["phrase"]):
                return {}
    gaps = {}
    labels = {"title": "标题", "rationale": "选材理由"}
    for path, reason in rejected:
        match = re.fullmatch(r"items\.(0|[1-9]\d*)\.(title|rationale)", path)
        if not match or int(match[1]) >= len(product.items) or any(f"items.{match[1]}.{f}" in locked for f in labels):
            return {}
        gaps.setdefault(int(match[1]), []).append(f"{labels[match[2]]}：{reason}")
    remaining = set(range(len(product.items))) - gaps.keys()
    if not remaining or any(f"items.{i}.{field}" not in approved for i in remaining for field in labels):
        return {}
    return {i: f"原第 {i + 1} 项未交付（{'；'.join(dict.fromkeys(reasons))}）。" for i, reasons in gaps.items()}


def review_groups(product, evidence):
    for index, item in enumerate(product.items):
        prefix = f"items.{index}."
        primary = primary_frame(item, evidence)
        citations = {key: evidence.video_byline.get(key, evidence[key]) if primary else evidence[key]
                     for key in item.sources}
        reads = {key.rsplit(":", 1)[0] for key in citations if key in evidence.points}
        fields = {prefix + "title": item.title, prefix + "rationale": item.rationale}
        frames = {prefix + "citations." + key: text for key, text in citations.items()
                  if key in evidence.points and evidence.display_source(key) == text}
        material = [key for key in item.sources if key in evidence.points]
        observed = {}
        if primary:
            read = primary.rsplit(":", 1)[0]
            for i, *_ in evidence.video_coverage[read]["samples"]:
                key = f"{read}:{i}"
                if key in evidence and evidence.points[key].cid == evidence.points[primary].cid:
                    observed[key] = evidence[key]
        yield fields, frames, {
            "items_to_verify": [{"index": index, "title": item.title, "rationale": item.rationale,
                                 "primary_frame": primary, "evidence": citations}],
            "field_visual_sources": dict.fromkeys(fields, material),
            "read_observations_for_screening": observed,
            "gaps": [], "video_read_coverage": {key: evidence.video_coverage[key] for key in reads},
        }
    if product.gaps:
        yield {f"gaps.{i}": gap for i, gap in enumerate(product.gaps)}, {}, {
            "items_to_verify": [], "gaps": product.gaps, "gap_evidence": evidence.prompt_sources(),
            "current_selection": [{"title": item.title, "primary_frame": primary_frame(item, evidence),
                                   "sources": item.sources} for item in product.items],
            "video_read_coverage": evidence.video_coverage,
        }


def correct_editorial_fields(draft, issues, replacements):
    product = PrivateDelivery.model_validate(draft)
    if set(replacements) != set(issues) or not set(issues) <= editorial_fields(product).keys():
        raise DelegationError("前台修正超出指定字段。")
    data = product.model_dump()
    for path, value in replacements.items():
        if not isinstance(value, str) or not value.strip():
            raise DelegationError("前台修正缺少文字。")
        parts = path.split(".")
        if parts[0] == "gaps":
            data["gaps"][int(parts[1])] = value
        else:
            data["items"][int(parts[1])][parts[2]] = value
    return PrivateDelivery.model_validate(data)


@dataclass(frozen=True)
class VideoPoint:
    bvid: str
    part: int
    cid: str | None
    seconds: float
    medium: str
    window: tuple[float, float] | None

    def supports(self, other):
        if not self.cid or (self.bvid, self.part, self.cid) != (other.bvid, other.part, other.cid):
            return False
        return self.seconds == other.seconds or (
            self.window is not None and self.window == other.window
        )

    def citation(self, item):
        coverage = self.medium
        if self.window:
            coverage += f"（抽样窗 {self.window[0]}–{self.window[1]} 秒）"
        # Older domain receipts sometimes linked only to the part. Deep-link to the actual sample.
        link = f"https://www.bilibili.com/video/{self.bvid}/?p={self.part}&t={int(self.seconds)}"
        return " · ".join([self.bvid, coverage, *_materials([item | {"url": link}])])

    def coordinate(self):
        return (f"{self.bvid} · {self.medium} · 分 P {self.part} · {self.seconds} 秒 · "
                f"<https://www.bilibili.com/video/{self.bvid}/?p={self.part}&t={int(self.seconds)}>")


class EvidenceIndex(dict):
    """The JSON view is the citable text. Validation uses the original receipt fields."""

    def __init__(self):
        super().__init__()
        self.points: dict[str, VideoPoint] = {}
        self.observations: dict[str, str] = {}
        self.provided_pages: dict[str, None] = {}
        self.read_pages: set[str] = set()
        self.video_coverage: dict[str, dict] = {}
        self.video_byline: dict[str, str] = {}
        self.part_bylines: dict[tuple[str, int, str], str] = {}

    @property
    def unread_pages(self):
        return [url for url in self.provided_pages if url not in self.read_pages]

    def prompt_sources(self):
        first = {}
        sources = {}
        for key, citation in self.items():
            original = first.setdefault(citation, key)
            sources[key] = citation if original == key else {"same_receipt_text_as": original}
        return sources

    def discovery_progress(self):
        # Reissued calls have new IDs. Equal coordinates and equal coverage are the same progress.
        bounds = {(read["bvid"], r["part"], r["start"], r["end"], bool(read.get("clip")))
                  for read in self.video_coverage.values() for r in read["requested_ranges"]
                  if all(k in r for k in ("part", "start", "end"))}
        points = {(p.bvid, p.part, p.cid or "", p.seconds, p.medium) for p in self.points.values()}
        return {"ranges": [list(row) for row in sorted(bounds)], "samples": [list(row) for row in sorted(points)]}

    def display_source(self, key):
        point = self.points.get(key)
        if point and point.medium == "单帧采样" and self.video_coverage[key.rsplit(":", 1)[0]].get("clip"):
            return point.coordinate()
        return self.video_byline.get(key, self[key])

    def referenced_body(self, body):
        if not body:
            return body
        body = "\n\n".join(block for block in body.split("\n\n") if not block.startswith(
            ("未核实或未覆盖：\n", "以下给定来源尚无网页正文回执：\n", "当轮取材窗口与观测覆盖：\n")))
        for citation, key in sorted({v: k for k, v in self.items()}.items(), key=lambda pair: -len(pair[0])):
            body = body.replace(citation, f"〔证据 {key}〕")
        return body

    def unused_video_material(self, previous, *, original=None, refinements=()):
        """Exclude old material for an explicit unused-window replacement, without asserting continuity."""
        original = self if original is None else original
        old_points = [(key.rsplit(":", 1)[0], original.points[key]) for key in previous]
        intervals = []
        for key in previous:
            point = original.points[key]
            bounds = original.video_coverage[key.rsplit(":", 1)[0]].get("explicit_interval")
            if bounds and bounds[1] - bounds[0] <= 10:
                intervals.append((key.rsplit(":", 1)[0], point.bvid, point.part, point.cid, *bounds))
        parents_by_read = {}
        if refinements:
            # Process longer parent intervals first so a second verified narrowing retains its ancestry.
            actual = [(read, entry["clip"]) for read, entry in self.video_coverage.items() if entry.get("clip")]
            for read, clip in sorted(actual, key=lambda pair: pair[1]["end"] - pair[1]["start"], reverse=True):
                parents = set()
                for proposal in refinements:
                    if clip["cid"] == proposal["cid"] and all(clip[k] == v for k, v in proposal["window"].items()):
                        parent = proposal["parent_read"]
                        parents.add(parent)
                        parents.update(parents_by_read.get(parent, ()))
                parents_by_read[read] = parents
        subset = EvidenceIndex()
        for key, text in self.items():
            point = self.points.get(key)
            if point:
                own_read = key.rsplit(":", 1)[0]
                own_interval = self.video_coverage[own_read].get("explicit_interval")
                parents = parents_by_read.get(own_read, ())
                if any(read not in parents and (old == point or old.supports(point)) for read, old in old_points) or any(
                    (point.bvid, point.part, point.cid) == (bvid, part, cid) and
                    (start <= point.seconds < end or own_interval and own_interval[0] < end and start < own_interval[1])
                    for read, bvid, part, cid, start, end in intervals if read not in parents
                ):
                    continue
                subset.points[key] = point
                subset.observations[key] = self.observations[key]
            subset[key] = text
        subset.provided_pages.update(self.provided_pages)
        subset.read_pages.update(self.read_pages)
        subset.video_coverage.update(self.video_coverage)
        subset.video_byline.update(self.video_byline)
        subset.part_bylines.update(self.part_bylines)
        return subset


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _video_point(item, step, result):
    if not _source(item.get("url")) or not _text(item.get("text")):
        return None
    url = urlsplit(item["url"])
    match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", url.path)
    part, seconds = item.get("part"), item.get("seconds")
    if url.netloc != "www.bilibili.com" or not match or type(part) is not int or part < 1 or not _time(seconds):
        return None
    bvid = match[1]
    query = parse_qs(url.query)
    arguments = step.get("arguments") or {}
    if (query.get("p", ["1"]) != [str(part)] or query.get("t", [str(int(seconds))]) != [str(int(seconds))]
            or arguments.get("part") not in (None, part)
            or any(binding.get("bvid", bvid) != bvid for binding in
                   (arguments, result.get("selection") or {}))):
        return None
    cid = str(item["cid"]) if item.get("cid") else None
    if item.get("kind") in {"字幕", "公开字幕抽样"} or result.get("viewing") == "subtitles_only":
        medium = "字幕时点"
    elif item.get("kind") in {"画面", "实际视频帧"} or result.get("viewing") == "visual_only":
        medium = "单帧采样"
    else:
        medium = "类型未核实的时点资料"
    window = item.get("sample_window")
    bounds = None
    if (medium == "单帧采样" and cid and isinstance(window, dict) and _time(window.get("start")) and _time(window.get("end"))
            and window["start"] <= seconds < window["end"]):
        bounds = (window["start"], window["end"])
    return VideoPoint(bvid, part, cid, seconds, medium, bounds)


def bind_delivery_schema(items, item_schema, evidence):
    if not evidence:
        items["maxItems"] = 0
        return
    props = item_schema["properties"]
    props["sources"]["items"]["enum"] = list(evidence)
    frames = [key for key, point in evidence.points.items() if point.medium == "单帧采样"]
    if frames:
        props["primary_frame"]["anyOf"][0]["enum"] = frames
    else:
        props["primary_frame"] = {"type": "null"}
        props["sources"]["minItems"] = 1


NON_NARRATIVE = ("仅频道、作者或节目题名与标识", "播出及制作资料", "评论收视或表演", "没有事件的布景描述")
WINDOW_REVIEW_VERSION = 5
NO_SPOILERS = "仅无剧透素材"
PROGRAMME_CONTEXT = "只谈片外的节目、制作、传播或可见形式"


def accepted_window(review, clip, strict):
    if (review.get("screening_version") != WINDOW_REVIEW_VERSION or review.get("clip") != clip
            or review.get("usage") not in {NO_SPOILERS, "允许讲述剧情或无此限制"} or review.get("accepted") is not True):
        return False
    coverage = review.get("coverage") or {}
    observations = review.get("observations") or []
    samples = coverage.get("samples") or []
    if (not coverage.get("complete") or coverage.get("uncovered") != [] or coverage.get("scanned_frames") != clip["frames"]
            or coverage.get("start") != clip["start"] or coverage.get("end") != clip["end"]
            or not observations or observations[0].get("frame") != 0 or observations[-1].get("frame") != clip["frames"] - 1
            or [(o.get("frame"), o.get("seconds")) for o in observations] != [(s["frame"], s["seconds"]) for s in samples]
            or any(o.get("scene") != "符合要求" or o.get("captions") not in {"无烧录字幕", "已读且符合"}
                   or not isinstance(o.get("screen_text"), str)
                   or o["captions"] == "已读且符合" and not o["screen_text"].strip() for o in observations)):
        return False
    topics = review.get("caption_topics")
    if not isinstance(topics, list) or any(not isinstance(t, dict) or not t.get("text") for t in topics):
        return False
    if {o["screen_text"].strip() for o in observations if o["screen_text"].strip()} != {t["text"] for t in topics}:
        return False
    if strict or review.get("usage") == NO_SPOILERS:
        return review.get("usage") == NO_SPOILERS and all(
            t.get("topic") in NON_NARRATIVE and t.get("subject") == PROGRAMME_CONTEXT and
            "story_fact" in t and t["story_fact"] is None for t in topics)
    return True


def _physical_clip(result, bounds, samples):
    clip = result.get("clip")
    if not isinstance(clip, dict) or not bounds or clip.get("audio") != "visual_only":
        return None
    start, end, frames = clip.get("start"), clip.get("end"), clip.get("frames")
    if not (_time(start) and _time(end) and bounds["start"] <= start < end <= bounds["end"]
            and type(frames) is int and frames >= 2 and type(clip.get("part")) is int
            and bounds["part"] == clip["part"] and clip.get("bvid") == result["selection"].get("bvid")):
        return None
    cid = str(clip.get("cid") or "")
    if not cid or not any(point.cid == cid and point.part == clip["part"] and point.bvid == clip["bvid"]
                          and start <= point.seconds < end and point.medium == "单帧采样" for point in samples):
        return None
    return {"bvid": clip["bvid"], "part": clip["part"], "cid": cid, "start": start, "end": end,
            "frames": frames, "audio": clip["audio"]}


def evidence_index(steps):
    evidence = EvidenceIndex()
    recalled = set()
    steps = list(steps)
    has_clips = any(s["tool"] == "inspect_video" and (s.get("result") or {}).get("clip") for s in steps)
    for step in steps:
        result = step.get("result") or {}
        tool = step["tool"]
        evidence.provided_pages.update(dict.fromkeys(url for url in step.get("provided_page_urls", ()) if _source(url)))
        if tool in {"owner_attachment", "authorized_inbox_text"}:
            body = result["text"]
            for offset in range(0, len(body), 800):
                label = f"{_text(result['name'])} · 字符 {offset + 1}–{min(offset + 800, len(body))}"
                evidence[f"{step['id']}:{offset}"] = f"{label}：{_text(body[offset:offset + 800])}"
            continue
        if tool in {"web_search", "delegate_text", "owner_code_grant"}:
            continue
        if tool == "recall_memory":
            from ...persona.memory.recall import canonical_id, source_href

            for index, item in enumerate(result.get("items", []), 1):
                identity = canonical_id(item)
                if identity in recalled:
                    continue
                recalled.add(identity)
                evidence[f"{step['id']}:{index}"] = " · ".join(filter(None, (
                    _text(item["id"]), _text(item["text"][:1200]), _source(source_href(item)),
                )))
            continue
        if tool == "inspect_video" and result.get("status") in {"completed", "partial"}:
            samples = []
            for index, item in enumerate(result.get("findings", []), 1):
                if point := _video_point(item, step, result):
                    key = f"{step['id']}:{index}"
                    evidence.points[key] = point
                    evidence.observations[key] = item["text"]
                    evidence[key] = point.citation(item)
                    samples.append([index, point.part, point.seconds, point.medium])
            selection = result.get("selection") or {}
            args = step.get("arguments", {})
            scope = [r for r in selection.get("ranges", []) if r.get("part") == args.get("part") and
                     _time(args.get("start")) and _time(args.get("end")) and _time(r.get("start"))
                     and _time(r.get("end")) and args["start"] <= r["start"] < r["end"] <= args["end"]]
            evidence.video_coverage[step["id"]] = {
                "status": result["status"], "viewing": result.get("viewing"),
                "bvid": selection.get("bvid") or step.get("arguments", {}).get("bvid"),
                "requested_ranges": selection.get("ranges", []), "samples": samples,
                "explicit_interval": [scope[0]["start"], scope[0]["end"]] if len(scope) == 1 else None,
                "read_gaps": [gap for gap in result.get("gaps") or [] if isinstance(gap, str)],
            }
            if len(scope) == 1 and (clip := _physical_clip(result, scope[0], (
                    evidence.points[f"{step['id']}:{i}"] for i, *_ in samples))):
                evidence.video_coverage[step["id"]]["clip"] = clip
        elif result and not result.get("error"):
            lines, verified = _describe(tool, result)
            if verified:
                evidence[step["id"]] = " · ".join(line for line in lines if line)
                if tool == "web_page":
                    evidence.read_pages.add(result["url"])
                    # A clip cites a document for provenance. Its page body can disclose the plot.
                    if has_clips:
                        evidence.video_byline[step["id"]] = " · ".join(filter(None, (
                            _text(result.get("title")), _source(result["url"]))))
                    if credits := _text(programme_credit_text(result)):
                        evidence.video_byline[step["id"]] = f"{credits} {_source(result['url'])}"
                    if result.get("source_kind") == "bilibili_public_metadata":
                        # The adapter keeps its public byline and part metadata before the synopsis field.
                        header, marker, _ = result["excerpt"].partition(" 秒。投稿简介：")
                        if result.get("retrieval") == "archive":
                            header, marker, _ = result["excerpt"].partition(" 秒。")
                        if marker and header.startswith(f"投稿《{result.get('title')}》，UP 主："):
                            evidence.video_byline[step["id"]] = f"{_text(header)} 秒。 {_source(result['url'])}"
                            url = urlsplit(result["url"])
                            bv = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", url.path)
                            cid = re.search(r"，CID (\d+)，时长 [\d.]+$", header)
                            part = parse_qs(url.query).get("p", ["1"])
                            if url.hostname == "www.bilibili.com" and bv and cid and len(part) == 1 and part[0].isdigit():
                                evidence.part_bylines[bv[1], int(part[0]), cid[1]] = evidence.video_byline[step["id"]]
                    address = step.get("arguments", {}).get("web_fetch")
                    if address:
                        evidence.read_pages.add(address)
    return evidence


def primary_frame(item, evidence):
    if any(source not in evidence for source in item.sources):
        raise DelegationError("交付引用了没有回执的来源。")
    points = evidence.points
    timed = [key for key in dict.fromkeys(item.sources) if key in points]
    frames = [key for key in timed if points[key].medium == "单帧采样"]
    primary = item.primary_frame
    if primary is None:
        if len(frames) > 1:
            raise DelegationError("多个采样画面须指定一个主帧，并核对同窗回执。")
        if not frames:
            if len(timed) > 1 and any(points[key].medium != "字幕时点" for key in timed):
                raise DelegationError("时点资料的类型未经核实，无法作为同一项视频选材。")
            return None
        primary = frames[0]
    if primary not in frames:
        raise DelegationError("主帧须是本项引用的实际画面回执。")
    step = primary.rsplit(":", 1)[0]
    read = evidence.video_coverage[step]
    clip = read.get("clip")
    adjacent = set()
    if clip and clip["start"] <= points[primary].seconds < clip["end"] and points[primary].cid == clip["cid"]:
        sampled = sorted((seconds, f"{step}:{i}") for i, part, seconds, medium in read["samples"]
                         if medium == "单帧采样" and part == clip["part"] and clip["start"] <= seconds < clip["end"]
                         and points[f"{step}:{i}"].cid == clip["cid"])
        index = sampled.index((points[primary].seconds, primary))
        adjacent = {key for _, key in sampled[max(0, index-1):index+2]}
    unsupported = [key for key in timed if key != primary and key not in adjacent
                   and not points[primary].supports(points[key])]
    if unsupported:
        raise DelegationError(
            f"同项视频证据须来自主帧时点或同一已核实的抽样窗。primary_frame={primary} "
            f"（P{points[primary].part} · {points[primary].seconds:g} 秒），sources 中的 "
            f"{'、'.join(unsupported)} 缺少关联。请移除这些引用及依赖它们的描述，或另行取材后再提交。"
        )
    return primary


def previous_frame_items(task, evidence):
    retained = {}
    previous = task["plan"].get("previous_selection")
    rows = previous.get("items", []) if previous is not None else _previous_rendered_items(task, evidence)
    for row in rows:
        if not set(row["sources"]) <= evidence.keys():
            continue
        item = CreativeItem.model_validate(row)
        try:
            key = primary_frame(item, evidence)
        except DelegationError:
            continue  # Older editions could group unrelated frames. They cannot be reused as a clip.
        if key:
            retained[key] = item.model_dump() | {"primary_frame": key}
    return retained


def _previous_rendered_items(task, evidence):
    """Recover an older edition only when its original item can be reproduced verbatim."""
    body = task["plan"].get("previous_delivery")
    if not body:
        return []
    old = evidence_index([s for s in task.get("steps", []) if s["revision"] < task.get("revision", 0)])
    by_text = {}
    for key, text in old.items():
        if evidence.get(key) == text:
            by_text[text] = key if text not in by_text else None
            if (short := old.display_source(key)) != text:
                by_text[short] = key if short not in by_text else None
    rows = []
    for block in body.split("\n\n"):
        lines = block.splitlines()
        if (len(lines) < 4 or not lines[0].startswith("**") or not lines[0].endswith("**")
                or not lines[1].startswith("选材理由：") or not lines[2].startswith("主帧：")):
            continue
        primary = by_text.get(lines[2].removeprefix("主帧："))
        sources = [primary, *(by_text.get(line[2:]) for line in lines[4:] if line.startswith("- "))]
        if not all(sources):
            continue
        title, rationale = (re.sub(r"\\(.)", r"\1", text) for text in (lines[0][2:-2], lines[1].removeprefix("选材理由：")))
        item = CreativeItem(title=title, rationale=rationale, sources=sources, primary_frame=primary)
        _, rendered = render_private_delivery(PrivateDelivery(items=[item], gaps=[]), old)
        current = rendered.split("\n\n", 1)[0]
        # Earlier renderers could mix a compact byline with a verbatim source excerpt.
        if len(lines) == 3 + len(sources) and lines[:4] == current.splitlines()[:4] and all(
                line in {f"- {old[key]}", f"- {old.display_source(key)}"}
                for line, key in zip(lines[4:], sources[1:], strict=True)):
            rows.append(item.model_dump())
    return rows


def render_private_delivery(product, evidence):
    blocks = []
    table = product.table_columns if not evidence.points else None
    if table and product.items:
        blocks.append("| " + " | ".join(map(_text, table)) + " |\n| --- | --- |\n" +
                      "\n".join(f"| {_text(item.title)} | {_text(item.rationale)} |" for item in product.items))
    windows = []
    bylines = set()
    used = set()
    for item in product.items:
        primary = primary_frame(item, evidence)
        lines = [f"**{_text(item.title)}**"] if table else [f"**{_text(item.title)}**", f"选材理由：{_text(item.rationale)}"]
        if primary:
            point = evidence.points[primary]
            if origin := evidence.part_bylines.get((point.bvid, point.part, point.cid)):
                bylines.add(origin)
            identity = (point.bvid, point.part, point.seconds)
            if identity in used:
                raise DelegationError("不同选材重复使用了同一主帧。")
            used.add(identity)
            lines.extend([f"主帧：{evidence.display_source(primary)}", "覆盖：仅核实所列采样时点；连续画面及音轨均未经该画面回执核实。"])
            read = evidence.video_coverage[primary.rsplit(":", 1)[0]]
            interval = read.get("explicit_interval")
            clip = read.get("clip")
            reviewed = False
            if clip and point.cid == clip["cid"] and clip["start"] <= point.seconds < clip["end"]:
                windows.append(f"- 第 {len(blocks) + 1} 项 · {point.bvid} P{point.part} · 连续取材入点 {clip['start']:g} 秒、"
                               f"出点 {clip['end']:g} 秒（不含），保存 {clip['frames']} 个连续原帧。私有短片不含音轨。")
                review = product._window_reviews.get(primary.rsplit(":", 1)[0], {})
                reviewed = accepted_window(review, clip, strict=False)
                if reviewed:
                    times = "、".join(f"{s['seconds']:g}" for s in review["observations"])
                    windows.append(f"- 第 {len(blocks) + 1} 项 · 逐帧检测画面及下方屏字变化；"
                                   f"后台模型审读代表帧 {times} 秒，检测到的状态均有代表帧。")
            if interval:
                samples = sorted({t for _, part, t, medium in read["samples"] if part == point.part and
                                  medium == "单帧采样" and interval[0] <= t < interval[1]})
                if samples:
                    windows.append(f"- 第 {len(blocks) + 1} 项 · {point.bvid} P{point.part} · "
                                   f"请求抽样范围 [{interval[0]:g}, {interval[1]:g}) 秒。"
                                   f"{'初读取材观察' if reviewed else '内容语义仅观察'}"
                                   f" {len(samples)} 帧，实际时点为 {'、'.join(map(str, samples))} 秒。")
            for gap in read["read_gaps"]:
                windows.append(f"- 第 {len(blocks) + 1} 项的工具回执：{_text(gap)}")
        lines.extend(f"- {evidence.display_source(source) if primary else evidence[source]}"
                     for source in dict.fromkeys(item.sources) if source != primary)
        blocks.append("\n".join(lines))
    if windows:
        blocks.append("当轮取材窗口与观测覆盖：\n" + "\n".join(windows))
    missing_bylines = [s for s in sorted(bylines) if not any(s in block for block in blocks)]
    if missing_bylines:
        blocks.append("取材投稿署名：\n" + "\n".join(f"- {s}" for s in missing_bylines))
    if product.gaps:
        blocks.append("未核实或未覆盖：\n" + "\n".join(f"- {_text(gap)}" for gap in product.gaps))
    if evidence.unread_pages:
        blocks.append("以下给定来源尚无网页正文回执：\n" + "\n".join(f"- {_source(url)}" for url in evidence.unread_pages))
    if not blocks:
        raise DelegationError("交付缺少材料或缺口说明。")
    return bool(product.items), "\n\n".join(blocks)


def _describe(tool, result):
    if tool == "workshop_lookup":
        if metrics := result.get("metrics"):
            return [watch_answer(metrics)], metrics["available"]
        lines = []
        for item in result.get("items", []):
            lines.append(" · ".join(filter(None, (_text(item["title"]), item["identity"], item["state"],
                                                 _source(item.get("href"))))))
            for entry in item["entries"]:
                lines.append(" · ".join(filter(None, (_text(entry["text"]), _source(entry.get("href"))))))
        return lines or ["这次工坊查询没有取得对应记录。"], bool(lines)
    if tool == "publish_dynamic_batch":
        rows = result.get("items", [])
        labels = {"published": "已发", "not_sent": "没发", "unconfirmed": "待核对"}
        lines = []
        for item in rows:
            status = labels[item["status"]]
            evidence = _source(item.get("href")) or item.get("number") or _text(item.get("reason"))
            preview = _text(item["text"][:72]) or f"草稿 #{item['post_id']}"
            lines.append(f"{status} · {preview}" + (f" · {evidence}" if evidence else ""))
        return lines, any(item["status"] == "published" for item in rows)
    if tool == "private_code":
        if result.get("name") and "text" in result:
            return [f"工件 {_text(result['name'])}：{_text(result['text'])}"], True
        return ["未取得文件工件。"], False
    if tool == "inbox_received":
        return [f"UID {result['sender_uid']} 的来信已进入账号 {result['account_id']} 的收件箱。",
                f"消息 ID：{result['message_id']} · 收件箱条目：{result['inbox_id']}"], True
    if tool == "inspect_dynamic_draft":
        lines = ["这是草稿，不是发布回执。", _text(result.get("target")), _text(result.get("text"))]
        for source in result.get("sources", []):
            lines.extend([_text(source["text"]), _source(source.get("source_url"))])
            lines.extend(_source(item.get("url")) for item in source.get("public_sources", []))
        return lines, bool(result.get("sources") and result.get("binding") and result.get("text"))
    if tool == "publish_dynamic_draft":
        confirmed = result.get("status") == "published" and bool(result.get("receipt") and result.get("write_token"))
        return (["平台已确认该动态发布。", _text(result.get("target")), _text(result.get("text"))]
                if confirmed else ["原提交尚未确认发布。"]), confirmed
    if tool == "operation_status":
        value = OPERATIONS.get(result.get("state"))
        return [value or "运营状态暂不可查。"], bool(value)
    if tool == "live_status":
        platform = PLATFORM.get(result.get("state"))
        native = result.get("native_program")
        lines = [f"平台直播{platform or '状态暂不可查'}。"]
        program = PROGRAM.get(native.get("state")) if isinstance(native, dict) else None
        if native is not None:
            lines.append(f"Omni 节目{program or '状态暂不可查'}。")
        return lines, bool(platform) and (native is None or bool(program))
    if tool == "web_search":
        sources = [source for link in result.get("links", []) if (source := _source(link))]
        return (["候选链接，待读取正文核验。", *sources] if sources else ["未找到候选来源。"]), bool(sources)
    if tool in {"web_page", "private_browser"}:
        origin, excerpt = _source(result.get("url")), _text(result.get("excerpt"))
        coverage = "来源摘录，内容有截断。" if result.get("truncated") else "来源摘录。"
        return [coverage, _text(result.get("title")), origin, excerpt], bool(origin and excerpt)
    if tool == "bilibili_owner":
        name, uid, origin = _text(result.get("name")), _text(result.get("uid")), _source(result.get("url"))
        return [f"{name}（UID {uid}）", origin] if name and uid and origin else ["缺少已核验的号主资料。"], bool(name and uid and origin)
    if tool == "operating_account":
        name, uid, origin = _text(result.get("name")), _text(result.get("uid")), _source(result.get("homepage"))
        if name and uid and origin:
            label = "上次核验的运营账号" if result.get("verification_stale") else "当前运营账号"
            return [f"{label} {name}（UID {uid}）", origin], True
        return ["暂时读不到已核验的 B 站运营账号。"], False
    if tool == "today_activity":
        date = _text(result.get("date"))
        events = [item | {"url": (item.get("source") or {}).get("href")} for item in result.get("events") or []
                  if isinstance(item, dict)]
        return [date, *(_materials(events) or ["当前记录没有可列出的运营事件。"])], bool(date)
    if tool == "inspect_video":
        done = result.get("status") == "completed"
        details = _materials(result.get("findings", []))
        note = "已取得时点取材回执。" if done and details else "没有足以交付的取材片段。"
        viewing = result.get("viewing")
        coverage = VIEWING.get(viewing, _text(viewing)) if isinstance(viewing, str) else ""
        return [note, coverage, _source(result.get("url")), *details], bool(done and details)
    if tool == "live_handoff":
        spoken = _text(result.get("spoken"))
        done = result.get("status") == "completed" and bool(spoken)
        return ["播放器确认播完。", f"台词为「{spoken}」"] if done else ["未收到播放器的播完确认。"], done
    if tool == "game_status":
        summary = _text((result.get("state") or {}).get("summary"))
        return [summary or "未取得模拟局面。"], result.get("simulated") is True and bool(summary)
    if tool == "game_action":
        done = result.get("success") is True
        return ["模拟游戏确认动作已执行。" if done else "模拟游戏未确认执行。", _text(result.get("state"))], done
    failed = result.get("status") in {"cancelled", "failed", "blocked", "partial"} or result.get("success") is False
    return ["工具未确认完成。" if failed else "已保存工具回执。"], bool(result) and not failed


def deliver_from_receipts(steps, revision, *, required_tools=(), reviewed_tools=()):
    results = {}
    for step in steps:
        if step["revision"] != revision or not isinstance(step.get("result"), dict):
            continue
        tool = step["tool"]
        key = step["id"] if tool in reviewed_tools else (tool, json.dumps(step.get("arguments", {}), sort_keys=True))
        results[key] = step

    blocks, gaps, observed = [], [], set()
    for step in results.values():
        tool, result = step["tool"], step["result"]
        label = LABELS.get(tool, _text(tool))
        error = _text(result.get("error"))
        lines, verified = ([error], False) if error else _describe(tool, result)
        blocks.append("\n".join([f"**{label}**", *(f"- {line}" for line in lines if line)]))
        if verified:
            observed.add(tool)
        elif tool != "web_search" or error:
            gaps.append(label)
    missing = set(required_tools) - observed
    gaps.extend(LABELS.get(tool, tool) for tool in sorted(missing))
    if not (observed - {"web_search"}):
        blocks.insert(0, "本轮未取得可交付的已核验工具结果。")
    if gaps:
        blocks.insert(0, "待核对的步骤为" + "、".join(dict.fromkeys(gaps)) + "。")
    return bool(observed - {"web_search"}) and not gaps, "\n\n".join(blocks)


def waiting_delivery(steps):
    checkpoint = (steps[-1].get("checkpoint") or {}) if steps else {}
    return checkpoint.get("interaction") or checkpoint.get("status") == "needs_reconciliation"


def watch_answer(stats):
    if not stats["available"]:
        return "当前读不到运营账号的观看记录。"
    text = f"{stats['day']} 有 {stats['completed']} 条视频读到了内容"
    if stats["completed"]:
        text += f"，其中 {stats['fully_read']} 条完整读完"
    if stats["in_progress"]:
        text += f"。还有 {stats['in_progress']} 条正在读取或等待继续"
    if stats["without_result"]:
        text += f"。另外 {stats['without_result']} 条已开始，但没有读到内容"
    return text + "。"
