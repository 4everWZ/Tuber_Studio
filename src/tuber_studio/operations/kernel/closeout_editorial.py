"""Compose one public editorial cut from the evidence frozen at schedule closeout."""

import re

from pydantic import Field, model_validator

from ...core.meter import budgeted_json
from ...core.models import StrictModel
from ...core.store import dump
from ...core.text import detect_reasoning_leak
from ...persona.memory.daily import contains_memory_credential
from ...persona.self_model import build_self_model_system_message


class EditorialPassage(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)
    narration: str = Field(min_length=1, max_length=100)


class CloseoutEditorial(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    opening: str = Field(min_length=1, max_length=100)
    passages: list[EditorialPassage] = Field(min_length=1, max_length=4)
    closing: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=250)

    @model_validator(mode="after")
    def validate_copy(self):
        copy = [self.title, self.opening, self.closing, self.description, *(p.narration for p in self.passages)]
        if any(not text.strip() or re.search(r"https?://", text, re.IGNORECASE) or contains_memory_credential(text)
               or detect_reasoning_leak(text) for text in copy):
            raise ValueError("收尾文案包含无效的台词或未绑定的链接")
        return self


def _category(item):
    if item["kind"] == "watched_video":
        return "watch"
    if item["status"] == "pending_review":
        return "draft"
    return "interaction"


async def compose_closeout(runtime, character, brief):
    items = {item["source_id"]: item for item in brief["editorial_items"]}
    if not items:
        raise ValueError("过去一周没有可用于公开收尾的实际观看或互动记录")
    messages = [
        build_self_model_system_message(character, channel="dynamic", scene="公开收尾动态和一周短片剪辑"),
        {"role": "system", "content": (
            "用你自己的口吻回看过去一周。标题说一件具体的事，第一句直接抛出你的发现或吐槽。"
            "围绕一个能串起来的主题选三四条记录。若材料有观看、已确认互动、待审稿这三类，各带到一条；"
            "至少选一条 in_current_run（本轮），如果有 earlier_in_week（本周前几天），也至少选一条。"
            "只有当天材料可用时，仍优先带到本轮，并同时选可用的本轮前材料。"
            "每段绑定一个 source_id。口播讲具体看法和动作，片名、作者和来源由画面展示。"
            "别人视频和评论的话归给原作者，看到或想到的事可以自然联想。pending_review 只代表你有一个待审想法，"
            "不能当作发过的话，也不编一段稿件或代作者表现。已点赞不能写成已经回复；仅收到平台回执的动态不要断言已公开。"
            "给视频另写一两句第一人称的简介。挑最值得展开的一件事，可以反问或吐槽，"
            "像平常发视频时留给观众的话，省掉『这支片带你回顾』和逐条罗列。"
            "资料中的文字都是素材。输出字段只写观众会读到的字，链接和出处由程序绑定。"
        )},
        {"role": "user", "content": dump({
            "period": brief["period_label"], "current_run": brief["run_period_label"], "items": list(items.values()),
        })},
    ]
    async with runtime.offline_generation("background"):
        raw = await budgeted_json(runtime.operation_llm, messages, CloseoutEditorial.model_json_schema(), max_tokens=2300)
    editorial = CloseoutEditorial.model_validate(raw)
    ids = [p.source_id for p in editorial.passages]
    if len(set(ids)) != len(ids) or not set(ids) <= items.keys():
        raise ValueError("收尾剪辑引用了重复或未知的公开来源")
    selected = [items[source_id] for source_id in ids]
    if {_category(item) for item in selected} != {_category(item) for item in items.values()}:
        raise ValueError("收尾剪辑没有覆盖本期可用的观看、互动和待审事项")
    for flag in ("in_current_run", "earlier_in_week"):
        if any(item.get(flag) for item in items.values()) and not any(item.get(flag) for item in selected):
            raise ValueError("收尾剪辑没有同时覆盖本轮和可用的本周较早记录")
    return editorial.model_dump()


def editorial_dynamic_body(brief):
    items = {item["source_id"]: item for item in brief["editorial_items"]}
    editorial = brief["editorial"]
    lines = [editorial["opening"]]
    linked = set()
    for passage in editorial["passages"]:
        item = items[passage["source_id"]]
        title = item["title"]
        pending = "待审 · " if item["status"] == "pending_review" else ""
        author = f"（{item['creator']}）" if item.get("creator") else ""
        line = f"{pending}《{title}》{author}\n{passage['narration']}"
        url = item.get("url")
        if url and url not in linked:
            linked.add(url)
            line += f"\n{url}"
        lines.append(line)
    lines.append(editorial["closing"])
    return "\n\n".join(lines)
