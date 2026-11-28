"""Compose one public editorial cut from the evidence frozen at schedule closeout."""

import re
from typing import Literal

from pydantic import Field, model_validator

from ...core.meter import budgeted_json
from ...core.models import StrictModel
from ...core.store import dump
from ...core.text import detect_reasoning_leak
from ...persona.memory.daily import contains_memory_credential
from ...persona.self_model import build_self_model_system_message

_BANNED_DYNAMIC_PHRASES = ("待审 ·", "待审·", "待审", "收到回执", "公开页读回", "运营收尾")
_MIN_SPOKEN_CHARS = 520
_MAX_SPOKEN_CHARS = 1350


class EditorialPassage(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)
    section: Literal["deep_dive", "side_cut"] = "deep_dive"
    chapter_title: str = Field(default="", max_length=40)
    template_hint: Literal[
        "QuoteSpotlight",
        "UIShowcase",
        "ChartCompare",
        "MemeCutaway",
        "TimelineSummary",
        "KineticStatement",
    ] | None = None
    narration: str = Field(min_length=30, max_length=320)
    key_points: list[str] = Field(default_factory=list, max_length=4)


class CloseoutEditorial(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    opening: str = Field(min_length=20, max_length=220)
    passages: list[EditorialPassage] = Field(min_length=3, max_length=10)
    closing: str = Field(min_length=20, max_length=220)
    description: str = Field(min_length=1, max_length=300)
    dynamic_post: str = Field(min_length=15, max_length=450)
    dynamic_source_ids: list[str] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def validate_copy(self):
        required_texts = [
            self.title,
            self.opening,
            self.closing,
            self.description,
            self.dynamic_post,
            *(p.narration for p in self.passages),
        ]
        optional_texts = [
            *(p.chapter_title for p in self.passages if p.chapter_title),
            *(kp for p in self.passages for kp in p.key_points),
        ]
        if any(
            not text.strip()
            or re.search(r"https?://", text, re.IGNORECASE)
            or contains_memory_credential(text)
            or detect_reasoning_leak(text)
            for text in required_texts
        ):
            raise ValueError("收尾文案包含无效的台词或未绑定的链接")
        if any(
            not text.strip()
            or re.search(r"https?://", text, re.IGNORECASE)
            or contains_memory_credential(text)
            or detect_reasoning_leak(text)
            for text in optional_texts
        ):
            raise ValueError("收尾分段要点包含无效文案或未绑定的链接")
        if any(phrase in self.dynamic_post for phrase in _BANNED_DYNAMIC_PHRASES):
            raise ValueError("收尾动态正文不能包含内部审核或回执术语")
        if len(set(self.dynamic_source_ids)) != len(self.dynamic_source_ids):
            raise ValueError("收尾动态不能重复引用同一来源")
        if sum(1 for p in self.passages if p.section == "deep_dive") < 2:
            raise ValueError("收尾视频至少需要 2 个核心话题深聊段落")
        spoken_chars = (
            len(re.sub(r"\s+", "", self.opening))
            + sum(len(re.sub(r"\s+", "", p.narration)) for p in self.passages)
            + len(re.sub(r"\s+", "", self.closing))
        )
        if not _MIN_SPOKEN_CHARS <= spoken_chars <= _MAX_SPOKEN_CHARS:
            raise ValueError(f"收尾视频口播字数需在 {_MIN_SPOKEN_CHARS} 至 {_MAX_SPOKEN_CHARS} 字之间（当前 {spoken_chars} 字）")
        return self


def _category(item):
    if item["kind"] == "watched_video":
        return "watch"
    if item["status"] == "pending_review":
        return "draft"
    return "interaction"


async def compose_closeout(runtime, character, brief):
    items = {item["source_id"]: item for item in brief["editorial_items"]}
    if not items or not any(item["status"] != "pending_review" for item in items.values()):
        raise ValueError("过去一周没有可用于公开收尾的实际观看或互动记录")
    messages = [
        build_self_model_system_message(character, channel="dynamic", scene="公开收尾动态和一周深度复盘视频"),
        {"role": "system", "content": (
            "用你自己的口吻回看过去一周，同时输出两部分独立内容：\n"
            "【一、1.5~5 分钟深度复盘视频脚本（总口播字数控制在 550~1200 字，采用「核心话题深聊 + 顺带本周切片」结构）】\n"
            "1. title 抛出一件具体的事；opening（60~160 字）直接用强钩子扔出你这周最戳人的发现或吐槽。\n"
            "2. passages 写 4~8 段，分成 2~3 个深聊章节（section='deep_dive'，每章设清晰的 chapter_title）和 1~2 个本周切片（section='side_cut'）：\n"
            "   - 深聊章节（每段 narration 110~240 字，配 2~4 条精炼 key_points 和合适的 template_hint）：把相互关联的视频细节、字幕观点和评论区争议拆开深聊，有理有据又有 Aqua 的吐槽味；\n"
            "   - 本周切片（每段 narration 60~130 字）：顺带拎一两条其他看过的视频或未发布的新选题想法。\n"
            "3. 若材料有观看、已确认互动、待审稿这三类，各带到一条；至少选一条 in_current_run（本轮），有 earlier_in_week（本周前几天）也至少选一条。\n"
            "4. closing（60~150 字）给出你的收尾想法，并留一个具体的反问邀请观众在评论区继续聊。\n"
            "5. description 用 1~2 句第一人称介绍这支片子，省掉『这支片带你回顾』和流水账罗列。\n"
            "【二、B 站日常公开动态（dynamic_post + dynamic_source_ids，必须与长视频脚本彻底分开写）】\n"
            "1. dynamic_post（60~260 字）写一条像真人随手发的日常吐槽或小记，只聊 1~2 个最真切的话题；\n"
            "   绝对禁止出现『待审』『待审 ·』『收到回执』『公开页读回』『运营收尾』等后台词，也不要罗列四条干巴巴的链接清单。\n"
            "2. dynamic_source_ids 只选 1~2 个 dynamic_post 实际聊到的公开 source_id，严禁选 status=='pending_review' 的草稿。\n"
            "【归因与真实性底线】\n"
            "别人视频和评论的话归给原作者，看到或想到的事可以自然联想。pending_review 只代表你有一个待审想法，"
            "不能当作发过的话，也不编一段稿件或代作者表现。已点赞不能写成已经回复；仅收到平台回执的动态不要断言已公开。"
            "所有输出字段只写观众会读到的字，不要写任何 http/https 链接，链接和出处由程序绑定。"
        )},
        {"role": "user", "content": dump({
            "period": brief["period_label"], "current_run": brief["run_period_label"], "items": list(items.values()),
        })},
    ]
    async with runtime.offline_generation("background"):
        raw = await budgeted_json(runtime.operation_llm, messages, CloseoutEditorial.model_json_schema(), max_tokens=5120)
    editorial = CloseoutEditorial.model_validate(raw)
    ids = [p.source_id for p in editorial.passages]
    passage_keys = [(p.source_id, p.section, p.chapter_title) for p in editorial.passages]
    if not set(ids) <= items.keys() or len(set(passage_keys)) != len(passage_keys):
        raise ValueError("收尾剪辑引用了重复或未知的公开来源")
    selected = [items[source_id] for source_id in dict.fromkeys(ids)]
    if {_category(item) for item in selected} != {_category(item) for item in items.values()}:
        raise ValueError("收尾剪辑没有覆盖本期可用的观看、互动和待审事项")
    for flag in ("in_current_run", "earlier_in_week"):
        if any(item.get(flag) for item in items.values()) and not any(item.get(flag) for item in selected):
            raise ValueError("收尾剪辑没有同时覆盖本轮和可用的本周较早记录")
    dyn_ids = editorial.dynamic_source_ids
    if not set(dyn_ids) <= items.keys():
        raise ValueError("收尾动态引用了未知的公开来源")
    if any(items[source_id]["status"] == "pending_review" for source_id in dyn_ids):
        raise ValueError("收尾动态不能把待审草稿当作公开事件来引用")
    return editorial.model_dump()


def editorial_dynamic_body(brief):
    items = {item["source_id"]: item for item in brief.get("editorial_items") or ()}
    editorial = brief["editorial"]
    post_text = str(editorial.get("dynamic_post") or "").strip()
    if not post_text:
        parts = [str(editorial.get("opening") or "").strip(), str(editorial.get("closing") or "").strip()]
        post_text = "\n".join(part for part in parts if part)

    selected_ids = list(editorial.get("dynamic_source_ids") or ())
    if not selected_ids:
        selected_ids = [
            passage["source_id"]
            for passage in editorial.get("passages", ())
            if passage.get("source_id") in items and items[passage["source_id"]].get("status") != "pending_review"
        ][:2]

    link_lines = []
    linked = set()
    for source_id in selected_ids[:2]:
        item = items.get(source_id)
        if not item or item.get("status") == "pending_review":
            continue
        url = item.get("url")
        if not url or url in linked:
            continue
        linked.add(url)
        title = item["title"]
        author = f"（{item['creator']}）" if item.get("creator") else ""
        link_lines.append(f"《{title}》{author} {url}".strip())

    if link_lines:
        return f"{post_text}\n\n" + "\n".join(link_lines)
    return post_text
