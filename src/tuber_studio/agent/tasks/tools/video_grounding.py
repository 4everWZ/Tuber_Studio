"""Video grounding, work-claim attribution, title form and clip screening helpers."""

import asyncio
import base64
import math
import re

import httpx

from ....core.store import dump
from ....infra.platforms.bilibili.frames import FfmpegTiles
from ....infra.web_client.fetch import programme_credit_text, source_url
from ..clip_review import review_clip_window, unreviewed_preview
from ..clip_screening import fit_review_group, observe_clip_batch
from ..delivery import (
    NO_SPOILERS,
    NON_NARRATIVE,
    PROGRAMME_CONTEXT,
    WINDOW_REVIEW_VERSION,
    DelegationError,
)
from ..handoff import video_findings
from ..public_video import clip_path
from ..video_scope import video_binding
from .schemas import (
    _CAPTION_TOPICS,
    _HTTPS_LINK,
    _NO_RETRY,
    ClipTitle,
    WorkClaim,
)


def video_provenance(steps, evidence):
    pages = [s for s in steps if s["tool"] == "web_page" and s["id"] in evidence]
    uploads = [s for s in pages if s["result"].get("source_kind") == "bilibili_public_metadata"]
    titles = [s["result"]["title"] for s in uploads if s["result"].get("title")]
    names = list(dict.fromkeys(name for title in titles for name in (re.findall(r"《([^《》]+)》", title) or [title])))
    sources = [s["id"] for s in pages if source_url(s["result"]["url"]).host != "www.bilibili.com" and any(
        name in (s["result"].get("title") or "") or name in (programme_credit_text(s["result"]) or "") for name in names)]
    return titles, names, [s["id"] for s in uploads], sources


def cited_programme_names(steps, source_ids):
    names = set()
    for step in steps:
        if step["id"] not in source_ids or step["tool"] != "web_page":
            continue
        receipt = step.get("result") or {}
        if receipt.get("excerpt") and not receipt.get("error"):
            names.update(re.findall(r"(?:^|；)节目名：([^；]+)", programme_credit_text(receipt) or ""))
    return names


async def review_work_claims(fields, generate, budget, *, known_names=(), visible_descriptions=()):
    subjects = ["构图、物件、外观或服装", "场内人物或道具的身份及所有者", "创作安排或其它表述",
                "含具体影视作品名称或将画面归属特定影片剧集"]
    subject_schema = {"type": "object", "properties": {k: {"type": "string", "enum": subjects} for k in fields},
                      "required": list(fields), "additionalProperties": False}
    classified = await generate([{"role": "system", "content": "逐字段判断文字谈论的对象。特定作品指可指认的电影、节目、"
        "剧名或其集次。讨论镜中有什么、某物属于谁、历史背景或怎么剪辑，分别按本义归类。"
        "一句兼有具体作品身份时选择含具体作品的一项。"},
        {"role": "user", "content": dump(fields)}], subject_schema, max_tokens=budget)
    if set(classified) != fields.keys() or any(kind not in subjects for kind in classified.values()):
        raise DelegationError("归属审读没有逐字段辨明讨论对象。")
    entities = {k: set(re.findall(r"《([^《》]+)》", v)) | {n for n in known_names if n in v} for k, v in fields.items()}
    relevant = {k: v for k, v in fields.items() if classified[k] == subjects[-1] or entities[k]}
    result = {k: [] for k in fields}
    if not relevant:
        return result
    # Extract possible referents independently of the reviewer who judges attribution.
    # A complete visual observation is not itself a proper name or an episode designation.
    mention = {"type": "object", "properties": {"span": {"type": "string"}, "kind": {"type": "string", "enum": [
        "特定作品专名", "具体影视作品或集次的指代", "人物地点或可见物体", "场景或一般表述"]}},
        "required": ["span", "kind"], "additionalProperties": False}
    spans = await generate([{"role": "system", "content": "按字段做实体识别。span 摘取原文里最小的专名、集次或显式节目指代，"
        "分别标明实体类型。镜内人物或物件的所有者只属于场内关系。普通名词和描述可以标为一般表述。"},
        {"role": "user", "content": dump(relevant)}], {"type": "object", "properties": {
            k: {"type": "array", "items": mention} for k in relevant}, "required": list(relevant),
            "additionalProperties": False}, max_tokens=budget)
    if set(spans) != relevant.keys():
        raise DelegationError("作品实体审查未返回对应字段。")
    for k, text in relevant.items():
        for entry in spans[k]:
            span = entry["span"].strip()
            if marked := re.fullmatch(r"《([^《》]+)》", span):
                span = marked[1]
            if (span and span in text and span not in visible_descriptions and
                    entry["kind"] in {"特定作品专名", "具体影视作品或集次的指代"}):
                entities[k].add(span)
    relevant = {k: v for k, v in relevant.items() if entities[k]}
    if not relevant:
        return result
    claim = WorkClaim.model_json_schema()
    claim["required"] = list(claim["properties"])
    schema = {"type": "object", "properties": {"work_claims": {"type": "object", "properties": {
        k: {"type": "array", "items": claim | {"properties": claim["properties"] | {
            "target": {"type": "string", "enum": sorted(entities[k])}}}} for k in relevant},
        "required": list(relevant), "additionalProperties": False}},
        "required": ["work_claims"], "additionalProperties": False}
    reviewed = await generate([{"role": "system", "content": "只读每个字段本身，提取它实际命名的作品，或明确把镜头、人物归到"
        "特定影视作品或集的文字，包括问句的前提。每条逐字引用 phrase 和其中的 target。"
        "区分画面上读到的海报题名、节目资料和镜头出处。字段若给作品定类型，逐字提取 work_type，phrase 包含它。"
        "一般布景、服饰、类型、道具属于谁、怎样剪辑均不表示作品身份。这些字段填空数组。"
        "不要替作者补充作品名、出处或镜头归属。"},
        {"role": "user", "content": dump(relevant)}], schema, max_tokens=budget)
    claims = reviewed["work_claims"]
    if set(claims) != relevant.keys():
        raise DelegationError("作品归属审查未逐项核对原字段。")
    if any(c["target"] not in entities[k] for k, entries in claims.items() for c in entries):
        raise DelegationError("审稿回包引用了字段中未识别的作品实体。")
    for k, entries in claims.items():
        for ref in entries:
            if ref.get("work_type") or ref["phrase"] == ref["target"]:
                continue
            # Read the qualifier in isolation. OCR transcription and category extraction are separate decisions.
            qualifier = await generate([{"role":"system", "content":"从原句中摘出用于限定这部作品媒介类型的原词。"
                "括号内或书名号内的是作品题名；海报、屏字是呈现形式。原句未限定作品类型时填空字符串。"},
                {"role":"user","content":dump({"原句":fields[k], "题名或作品指代":ref["target"]})}],
                {"type":"object","properties":{"type_word":{"type":"string"}}, "required":["type_word"],
                 "additionalProperties":False}, max_tokens=budget)
            word = qualifier["type_word"].strip()
            if word and word in fields[k]:
                ref["work_type"] = word
        for ref in entries:
            word = ref.get("work_type") or ""
            designation = f"《{ref['target']}》"
            if word.endswith(designation) and word != designation:
                ref["work_type"] = word.removesuffix(designation).strip()
        for missing in entities[k] - {c["target"] for c in entries}:
            entries.append(WorkClaim(phrase=missing, target=missing, relation="未能核实所述关系").model_dump())
    return result | claims


def attribution_receipt(path, text, claims, steps, item, *, screen_text=()):
    pages = [{"source": s["id"], "url": s["result"]["url"], "title": s["result"].get("title"),
              "programme_credits": programme_credit_text(s["result"]),
              "programme_names": sorted(cited_programme_names([s], item.sources))}
             for s in steps if s["tool"] == "web_page" and s["id"] in item.sources and (s.get("result") or {}).get("excerpt")
             and not s["result"].get("error")]
    names = {name for page in pages for name in page["programme_names"]}
    references = [WorkClaim.model_validate(c) for c in claims]
    for name in re.findall(r"《([^《》]+)》", text):
        if not any(c.target == name for c in references):
            references.append(WorkClaim(phrase=f"《{name}》", target=name, relation="提及节目资料"))
    checked = []
    for ref in references:
        issue = None
        typed_pages = [p for p in pages if ref.target in p["programme_names"]]
        types = {kind for p in typed_pages for kind in re.findall(r"(?:^|；)节目类型：([^；]+)", p["programme_credits"])}
        qualifiers = {ref.work_type} if ref.work_type else set()
        if ref.phrase not in text or ref.target not in ref.phrase or ref.work_type and ref.work_type not in ref.phrase:
            issue = "审核未能逐字定位该主张，归属判断尚未核实。"
        elif ref.relation == "断言镜头或人物的作品或集归属":
            issue = "引用的节目署名无法鉴定镜头或人物的作品或集归属。"
        elif ref.relation == "未能核实所述关系":
            issue = "尚未区分画面屏字、节目资料或镜头归属。"
        elif qualifiers and not qualifiers <= types:
            issue = "所述作品类型未经该作品的节目资料支持。"
        elif ref.relation == "转述画面可读题名":
            if not any(ref.target in line for line in screen_text):
                issue = "本主帧的屏字抄录没有该题名。"
        elif ref.target not in names:
            issue = "本项引用的已读节目署名未支持所写作品名。"
        checked.append(ref.model_dump() | {"issue": issue, "claimed_types": sorted(qualifiers), "catalogue_types": sorted(types),
            "screen_quotes": [line for line in screen_text if ref.target in line], "matching_programme_pages": [
            page["source"] for page in pages if ref.target in page["programme_names"]], "other_programme_pages": [
            page["source"] for page in pages if page["programme_names"] and ref.target not in page["programme_names"]]})
    return {"field": path, "text": text, "primary_frame": item.primary_frame, "claims": checked, "pages": pages}

def title_options(facts):
    return {"anyOf": [ClipTitle.model_json_schema()["properties"]["title"],
                      {"type": "object", "properties": {"use_fact": {"type": "string", "enum": facts}},
                       "required": ["use_fact"], "additionalProperties": False}],
            "description": "可自由拟题，也可逐字选用一条已观察的构图或细节。两种选择都须符合号主写法并通过原图审稿。"}


def selected_title(value, facts):
    if isinstance(value, dict):
        if set(value) != {"use_fact"} or value["use_fact"] not in facts:
            raise DelegationError("标题所选观察未绑定本项主帧。")
        value = value["use_fact"]
    return ClipTitle(title=value).title


async def requested_title_form(choice, instructions, generate, budget):
    expected = choice.get("requested_form")
    quote = choice.get("form_quote", "").strip()
    if not expected or expected == "不限" or not quote or not any(quote in text for text in instructions):
        return None
    forms = ["未规定标题句式","名词短语","陈述句","问句"]
    reply = await generate([{"role":"system","content":"读号主原话，只判断其明确要求标题用哪种句式。"
        "取几个片段、标题应写什么内容以及怎样使用素材等要求都不指定句式。"},
        {"role":"user","content":quote}], {"type":"object","properties":{
            "explicit_form":{"type":"string","enum":forms}},"required":["explicit_form"],"additionalProperties":False},
        max_tokens=budget)
    return expected if reply["explicit_form"] == expected else None


def unsupported_timecode(title, point, clip):
    """Check explicit part/time coordinates independently from on-screen clock text."""
    for match in re.finditer(r"(?i)(?:\bP|分\s*P)\s*(\d+)\s*[·,，/：:\s]\s*(\d+(?::\d{2}){1,2}(?:\.\d+)?)", title):
        seconds = 0.0
        for component in match[2].split(":"):
            seconds = seconds * 60 + float(component)
        valid = clip["start"] <= seconds < clip["end"] if clip else int(seconds) == int(point.seconds)
        if int(match[1]) != point.part or not valid:
            return match[0]
    return None


def pixel_caption_options(inventory, uses):
    """Present only observed details and the editor's proposed uses as distinct choices."""
    return {"anchor": inventory["labels"], "detail": inventory["details"], "use": uses}


def pixel_choice_schema(choices):
    return {"type": "object", "properties": {k: {"type": "string", "enum": values} for k, values in choices.items()},
            "required": list(choices), "additionalProperties": False}


def pixel_rationale(choices, answer):
    if any(answer.get(key) not in pool for key, pool in choices.items()):
        raise DelegationError("选材文字没有绑定独立查看主帧所得的构图及编辑安排。")
    return f"画面依据：{answer['detail'].rstrip('。')}。剪辑安排：{answer['use']}"


def apply_repair_candidate_receipts(disputed, fields, primary, receipts):
    """Keep the objection if the same rejected fact is reused verbatim on the same primary frame."""
    for path, text in fields.items():
        detail, separator, _ = text.partition("。剪辑安排：")
        fact = detail.removeprefix("画面依据：") if (
            path.endswith(".rationale") and detail.startswith("画面依据：") and separator
        ) else text
        for prior in receipts:
            if prior.get("stage") != "review_repair_candidate" or prior.get("primary_frame") != primary:
                continue
            candidate = prior["candidate"]
            if candidate.rstrip("。") != fact.rstrip("。"):
                continue
            located = [claim for claim in prior["claims"] if claim.get("target") and claim["target"] in fact]
            if not located:
                continue
            row = disputed.setdefault(path, {"field": path, "text": text, "primary_frame": primary,
                                             "claims": [], "pages": []})
            for claim in located:
                if claim not in row["claims"]:
                    row["claims"].append(claim)
            source = {"field": prior["field"], "candidate": candidate}
            if source not in row.setdefault("repair_candidate_sources", []):
                row["repair_candidate_sources"].append(source)

def unexplored_samples(evidence, reads, starts, seconds):
    """Bind each overview observation to a still available window that contains its actual sample."""
    by_part = {(s["arguments"]["bvid"], s["arguments"]["part"]): key for key, s in reads.items() if key in starts}
    proposals = {}
    for key, point in evidence.points.items():
        region = by_part.get((point.bvid, point.part))
        coverage = evidence.video_coverage[key.rsplit(":", 1)[0]]
        if not region or point.medium != "单帧采样" or coverage.get("clip"):
            continue
        center = math.floor(point.seconds - seconds / 2)
        for low, high in starts[region]:
            start = max(low, min(center, high))
            if start <= point.seconds < start + seconds:
                proposals[key] = {"window": {"bvid": point.bvid, "part": point.part, "start": start, "end": start + seconds},
                                  "cid": point.cid, "seconds": point.seconds, "observation": evidence.observations[key]}
                break
    return proposals


def attempted_windows(task):
    windows = []
    for step in task.get("steps", ()):
        if step["tool"] != "inspect_video":
            continue
        args = step["arguments"]
        if (args.get("part") and isinstance(args.get("start"), (float, int)) and isinstance(args.get("end"), (float, int))
                and 0 < args["end"] - args["start"] <= 10 and args not in windows):
            windows.append(args)
    return windows


def overlaps(left, right):
    return (left["bvid"] == right["bvid"] and left["part"] == right["part"]
            and left["start"] < right["end"] and right["start"] < left["end"])


def new_refinement(window, attempts):
    for old in attempts:
        if overlaps(window, old):
            if not old["start"] <= window["start"] < window["end"] <= old["end"]:
                return False
            if (old["start"], old["end"]) == (window["start"], window["end"]):
                return False
    return True


def unused_starts(task, reads, clips, seconds):
    bound = video_binding(task)
    allowed = {}
    for key, step in reads.items():
        args = step["arguments"]
        bv, part = args["bvid"], args["part"]
        if bv not in bound or (parts := bound[bv]) is not None and part not in parts:
            continue
        duration = step["result"]["part_duration"]
        regions = (parts or {}).get(part) or [(0, duration)]
        if "available_windows" in step["result"]:
            regions = [(max(lo, a), min(hi, b)) for lo, hi in regions
                       for a, b in step["result"]["available_windows"] if max(lo,a) < min(hi,b)]
        occupied = sorted((c["start"], c["end"]) for c in clips if c["bvid"] == bv and c["part"] == part)
        intervals = []
        for left, right in regions:
            right = min(right, duration)
            for start, stop in occupied:
                if stop <= left or right <= start:
                    continue
                if start - left >= seconds:
                    intervals.append([left, start - seconds])
                left = max(left, stop)
            if right - left >= seconds:
                intervals.append([left, right - seconds])
        if intervals:
            allowed[key] = intervals
    return allowed


def discovery_windows(candidate, evidence, seconds, target):
    """Turn actual nominated sample times into disjoint reads inside the observed source bounds."""
    windows = []
    if seconds > 10:
        return windows
    keys = [key for item in candidate["items"] for key in [item.get("primary_frame"), *item["sources"]] if key]
    for key in dict.fromkeys(keys):
        point = evidence.points.get(key)
        if not point or point.medium != "单帧采样":
            continue
        read = evidence.video_coverage[key.rsplit(":", 1)[0]]
        if read.get("clip"):
            continue
        for bounds in read["requested_ranges"]:
            if (bounds["part"] != point.part or not bounds["start"] <= point.seconds < bounds["end"]
                    or bounds["end"] - bounds["start"] < seconds):
                continue
            start = max(bounds["start"], min(math.floor(point.seconds - seconds / 2), bounds["end"] - seconds))
            end = start + seconds
            if not any(w["bvid"] == point.bvid and w["part"] == point.part and w["start"] < end and start < w["end"]
                       for w in windows):
                windows.append({"bvid": point.bvid, "part": point.part, "start": start, "end": end})
            break
        if len(windows) >= target:
            break
    return windows


def clip_options(proposed, evidence):
    options = []
    seen = set()
    origins = {}
    for item in proposed:
        point = evidence.points[item["primary_frame"]]
        origins.setdefault((point.bvid, point.part, point.cid), []).extend(k for k in item["sources"] if k not in evidence.points)
    for item in [*proposed, *({"primary_frame": k} for k in evidence.points)]:
        key = item["primary_frame"]
        point = evidence.points[key]
        clip = evidence.video_coverage[key.rsplit(":", 1)[0]].get("clip")
        if not clip or point.medium != "单帧采样" or not clip["start"] <= point.seconds < clip["end"]:
            continue
        media = (point.bvid, point.part, point.cid)
        window = (*media, clip["start"], clip["end"])
        if media in origins and window not in seen and point.cid == clip["cid"]:
            seen.add(window)
            options.append({"primary_frame": key, "sources": item.get("sources", [key, *dict.fromkeys(origins[media])])})
    return options


def saved_review_media(runtime_dir, task, evidence, primary):
    read_id = primary.rsplit(":", 1)[0]
    clip = evidence.video_coverage[read_id].get("clip")
    if not clip:
        return None
    steps = {s["id"]: s for s in task.get("steps", []) if s["tool"] == "inspect_video"}
    if read_id not in steps:
        return None
    media_id = (steps[read_id].get("result") or {}).get("request_id", read_id)
    if media_id not in steps or evidence.video_coverage.get(media_id, {}).get("clip") != clip:
        raise DelegationError("待核对主帧没有本任务的短片文件回执。")
    path = clip_path(runtime_dir, media_id)
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise DelegationError("已保存短片无法在对应入出点读回主帧。")
    return path, clip


async def clip_review_image(runtime_dir, task, evidence, primary):
    media = saved_review_media(runtime_dir, task, evidence, primary)
    if not media:
        return None
    path, clip = media
    return await decode_review_frame(path, clip, evidence.points[primary].seconds)


async def decode_review_frame(path, clip, seconds):
    delta = seconds - clip["start"]
    if not 0 <= delta < clip["end"] - clip["start"]:
        raise DelegationError("主帧时点不在已保存短片内。")
    decoder = FfmpegTiles()
    # Fragmented MP4 may carry a nonzero first PTS. Match an actual normalized presentation time.
    image = await decoder._run([decoder.ffmpeg, "-v", "error", "-threads", "1", "-protocol_whitelist", "pipe",
                               "-f", "mov", "-i", "pipe:0", "-vf", f"setpts=PTS-STARTPTS,select=lt(abs(t-{delta:.9f})\\,0.000001)",
                               "-frames:v", "1", "-an", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                               await asyncio.to_thread(path.read_bytes), 1024 * 1024)
    if not image:
        raise DelegationError("短片中没有落在主帧实际时码的画面。")
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}}


def retry_incomplete_previews(plan):
    reviews = plan.get("window_reviews", {})
    pending = {key for key, review in reviews.items() if unreviewed_preview(review)}
    if pending:
        # The ordinary next checkpoint must retire the corresponding exclusions too.
        plan["window_reviews"] = {key: review for key, review in reviews.items() if key not in pending}
        plan["excluded_video_frames"] = [key for key in plan.get("excluded_video_frames", ())
                                         if key.rsplit(":", 1)[0] not in pending]


async def screen_clip_window(media, goal, generate, budget, inherit_no_spoilers=False):
    path, clip = media
    observations, calls, limits = [], [], []

    async def observe_batch(samples, images):
        observations.extend(await observe_clip_batch(samples, images, goal, generate, budget, calls, limits))

    result = await review_clip_window(path, start=clip["start"], end=clip["end"], expected_frames=clip["frames"],
                                      max_images=None, on_batch=observe_batch)
    coverage = result["coverage"]
    coverage["model_batches"] = calls
    coverage["model_budget_splits"] = limits
    samples = coverage["samples"]
    receipt = {"clip": clip, "coverage": coverage, "observations": observations, "accepted": False, "screening_version": WINDOW_REVIEW_VERSION}
    if (not coverage["complete"] or coverage["uncovered"] or coverage["scanned_frames"] != clip["frames"]
            or coverage.get("start") != clip["start"] or coverage.get("end") != clip["end"]
            or not samples or samples[0]["frame"] != 0 or samples[-1]["frame"] != clip["frames"] - 1
            or [(s["frame"], s["seconds"]) for s in observations] != [(s["frame"], s["seconds"]) for s in samples]
            or any(o["scene"] != "符合要求" or o["captions"] not in {"无烧录字幕", "已读且符合"}
                   or o["captions"] == "已读且符合" and not o["screen_text"].strip() for o in observations)):
        return receipt
    return await classify_clip_captions(receipt, goal, generate, budget, inherit_no_spoilers)


async def classify_clip_captions(receipt, goal, generate, budget, inherit_no_spoilers):
    # The OCR model can transcribe a plot event correctly and still call the clip suitable.
    # Classify each actual distinct line independently of that visual verdict.
    lines = dict(enumerate(dict.fromkeys(o["screen_text"].strip() for o in receipt["observations"] if o["screen_text"].strip()), 1))
    allowed = (NO_SPOILERS, "允许讲述剧情或无此限制")
    usage = {"usage": NO_SPOILERS} if inherit_no_spoilers else await generate([{"role": "system", "content": "仅确定号主对这批素材的剧情披露要求。"},
                            {"role": "user", "content": goal}],
                           {"type": "object", "properties": {"usage": {"type": "string", "enum": list(allowed)}},
                            "required": ["usage"], "additionalProperties": False}, max_tokens=budget)
    if usage.get("usage") not in allowed:
        raise DelegationError("未确定当前交办的剧情披露范围。")
    subjects = (PROGRAMME_CONTEXT, "提及片中人物或所讲世界中发生的事", "混合或无法辨明")
    field = {"type": "object", "properties": {"topic": {"type": "string", "enum": list(_CAPTION_TOPICS)},
             "subject": {"type": "string", "enum": list(subjects)}}, "required": ["topic", "subject"], "additionalProperties": False}

    async def classify_group(keys):
        schema = {"type": "object", "properties": {str(k): field for k in keys},
                  "required": list(map(str, keys)), "additionalProperties": False}
        topics = await generate([{"role": "system", "content": "标注字幕的叙述层次。片外是节目本身的制作、播出、收视和观众评价，"
            "这些可以有时间和动作，但不是片中世界的事实。片内是作品讲述的人物和事件；提问、假设或评论中提到的片内事件也标出。"
            "例：‘观众称它是年度佳作’是节目评价，谈片外；‘今年重映’是播出资料，谈片外。"
            "‘桥断后村民去了哪儿’提及故事中‘桥断’，应标事件；‘他推开门’标人物动作。"},
            {"role": "user", "content": dump({"待标注原句": {k: lines[k] for k in keys}})}], schema, max_tokens=budget)
        if set(topics) != set(map(str, keys)):
            raise DelegationError("字幕分类未逐条绑定实际审读原文。")
        return topics

    receipt["caption_budget_splits"] = []
    topics = await fit_review_group(list(lines), classify_group, receipt["caption_budget_splits"]) if lines else {}
    receipt["caption_topics"] = []
    for k, line in lines.items():
        entry = topics[str(k)]
        if not isinstance(entry, dict) or entry.get("topic") not in _CAPTION_TOPICS or entry.get("subject") not in subjects:
            receipt["classification_gap"] = "字幕审查未能引用该行实际原文。"
            return receipt
        receipt["caption_topics"].append({"text": line, **entry, "story_fact": None if entry["subject"] == subjects[0] else line})
    receipt["usage"] = usage["usage"]
    receipt["accepted"] = receipt["usage"] != allowed[0] or all(
        item["topic"] in NON_NARRATIVE and item["story_fact"] is None for item in receipt["caption_topics"])
    return receipt


def sampled_video_findings(finding, part):
    materials = video_findings(finding, part=part)
    windows = {}
    cids = {}
    for entry in (finding.get("coverage") or {}).get("parts", []):
        number = entry["part"]
        cids[number] = entry.get("cid")
        for interval in entry.get("ranges", []):
            for window in (interval.get("visual") or {}).get("windows", []):
                if window.get("state") == "sampled":
                    for seconds in window["samples"]:
                        windows.setdefault((number, seconds), set()).add((window["start"], window["end"]))
    for item in materials:
        item["cid"] = cids.get(item["part"])
        bounds = windows.get((item["part"], item["seconds"]))
        if item.get("kind") == "画面" and bounds and len(bounds) == 1:
            start, end = next(iter(bounds))
            item["sample_window"] = {"start": start, "end": end}
    return materials

def available_web_addresses(goal, steps):
    candidates = [match.rstrip(".,;!?)") for match in _HTTPS_LINK.findall(goal)]
    read = set()
    no_retry = bool(_NO_RETRY.search(goal))
    for step in steps:
        result = step.get("result", {})
        if step["tool"] == "web_search" and not result.get("error"):
            candidates.extend(result.get("links", []))
        if step["tool"] == "web_page" and (result.get("excerpt") or no_retry):
            address = step.get("arguments", {}).get("web_fetch")
            if isinstance(address, str):
                read.add(str(source_url(address)))
    links = {}
    for candidate in candidates:
        if not isinstance(candidate, str) or len(candidate) > 2048:
            continue
        try:
            link = source_url(candidate)
        except httpx.InvalidURL:
            continue
        if link.scheme == "https" and link.host and not link.userinfo and str(link) not in read:
            links.setdefault(str(link), None)
    return list(links)
