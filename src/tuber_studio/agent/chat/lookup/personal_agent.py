"""Verified conversation objects and read-only research for the personal Agent."""

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

import httpx
from pydantic import Field

from ....core.models import StrictModel, content_hash
from ...goals.comment_events import CommentWatch
from ...goals.events import EventGrant
from ...goals.followups import FollowupGrant, StatusSource
from ...tasks.public_sources import published_on
from . import operator_facts


@dataclass(frozen=True)
class Capability:
    subject: str
    reads: str
    changes: str = ""
    read_actions: tuple[str, ...] = ()
    change_actions: tuple[str, ...] = ()
    selection_action: str | None = None


CAPABILITIES = {
    "identity": Capability("当前角色、绑定主人、运营登录账号", "身份、UID、主页、已核验公开资料",
                           read_actions=("owner_binding", "owner_profile", "operating_account")),
    "settings": Capability("日常设置、画布、点歌与宠物", "当前字段、可用选项", "按权限保存指定字段",
                           ("workbench_capabilities", "daily_settings", "pet_settings"), ("daily_settings", "pet_settings")),
    "operations": Capability("运营、公开活动和平台回执", "运行状态、经历、来源、作者、送达", "开启、定时／时间段排班、调整或停止自主运营",
                             ("activity", "event_details", "event_author", "event_reply", "video_activity", "operation_status"),
                             ("start_operations", "schedule_operations", "operation_control", "stop")),
    "outgoing": Capability("动态、评论回复、私信的待发草稿与提交记录", "查看队列，或查已交办发布的实际进度",
                           "把已有动态草稿交给发布工具准备确认。创建动态草稿，或拟定已发布动态的修改",
                           ("outgoing_queue", "task_status"), ("prepare_publication", "dynamic", "edit_dynamic"),
                           selection_action="prepare_publication"),
    "video": Capability("视频和分 P", "已读材料、时码、读取进度", "刷视频、整段取材、续看或精查指定范围",
                        ("video_activity", "video_details"), ("browse", "full_video", "resume_video", "stop")),
    "live": Capability("直播和点歌播放", "场次、节目、播放和回执", "交接台词、调整直播习惯或停止直播",
                       ("live_status", "live_history"), ("start_live", "live_handoff", "live_preference", "stop")),
    "game": Capability("当前游戏会话", "局面与控制权", "由场景授权控制", ("game_status",), ("game_handoff",)),
    "memory": Capability("长期记忆与经历", "可见偏好、来源、公开经历", "保存、更新或忘记指定记忆",
                         ("public_experiences", "recall_experience", "memory_summary"),
                         ("remember_event", "forget_memory", "save_memory", "update_memory", "delete_memory")),
    "task": Capability("交办、长期目标和成果", "进度、计划、等待原因、版本", "修改、暂停、停止或安排跟进",
                       ("task_status",), ("pause_task", "resume_task", "watch_goal")),
    "web": Capability("公开网页与当下外部事实", "搜索并打开来源，核对发布日期", read_actions=("web_lookup",)),
    "clock": Capability("本地时钟", "本地日期与时间", read_actions=("clock",)),
}

class ConversationObject(StrictModel):
    kind: Literal["owner", "account", "setting", "operations", "event", "video", "watch_history", "watched_video", "public_comment",
                  "dynamic_post", "live", "game", "memory", "task", "artifact", "web", "outgoing", "recalled_source"]
    identity: str = Field(min_length=1, max_length=2048)
    label: str = Field(max_length=200)
    revision: str | int | None = None
    part: int | None = None
    seconds: float | None = None


class PublicSearchTerms(StrictModel):
    terms: list[str] = Field(max_length=4,
                             description="只摘当前原话中的地区、机构、人物、主题或专名，按查询权重排序。")
    localized: str = Field(max_length=120, description="首个有效主题词的繁中检索写法，无主题词时为空。")


class Arrangement(StrictModel):
    kind: Literal["once", "daily", "weekly", "inbox", "comment"]
    quote: str = Field(min_length=2, max_length=300)
    local_date: date | None = None
    local_time: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int | None = Field(default=None, strict=True, ge=0, le=6)
    states: list[StatusSource] = Field(default_factory=list, max_length=4)
    sender_uid: str | None = Field(default=None, pattern=r"^[1-9]\d{0,19}$")
    resource: str | None = Field(default=None, max_length=32)
    root_id: str | None = Field(default=None, pattern=r"^[1-9]\d{0,19}$")
    after_reply: str | None = Field(default=None, min_length=1, max_length=400,
                                    description="仅在对方明确交代来信后的私有文本工作时，逐字摘录该交付要求。")

    def due(self, message, now):
        if (self.quote not in message or not self.local_date or not self.local_time
                or not re.search(r"\d|[一二三四五六七八九十零两]点|明天|后天|上午|下午|中午|晚上", self.quote)):
            raise ValueError("请说一下具体哪一天、几点执行。")
        wall = datetime.fromisoformat(f"{self.local_date}T{self.local_time}")
        local = wall.replace(tzinfo=now.tzinfo)
        if (local <= now or (local.date() - now.date()).days > 366
                or datetime.fromtimestamp(local.timestamp(), now.tzinfo).replace(tzinfo=None) != wall):
            raise ValueError("请提供本地时区内一年内的有效未来时间。")
        return local.timestamp()

    def configure(self, tasks, goal_id, message, now):
        if self.quote not in message or not (task := tasks.executable(goal_id)):
            raise ValueError("请指定当前授权下的长期目标及跟进条件。")
        if self.after_reply and (self.kind != "inbox" or self.after_reply not in self.quote):
            raise ValueError("请说明收到这封来信后要完成的具体私有文本工作。")

        def as_goal():
            if not tasks.workspace.goal(goal_id):
                tasks.workspace.promote(task)

        epoch = tasks.scope()[-1]
        if self.kind in {"daily", "weekly"}:
            cadence = r"每[天日]|天天|逐日" if self.kind == "daily" else r"每周|每星期|每个星期|逐周"
            if not re.search(cadence, self.quote):
                raise ValueError("请明确指定跟进频率。")
            source_words = {"live_status": "直播|节目", "game_status": "游戏|局面",
                            "operation_status": "运营", "today_activity": "活动|经历|观看记录"}
            if any(not re.search(source_words[name], self.quote) for name in self.states):
                raise ValueError("请明确指定要持续关注的状态或来源。")
            pages = tasks.unread_web_addresses(self.quote, ())
            grant = FollowupGrant(epoch=epoch, cadence=self.kind, local_time=self.local_time or "",
                                  timezone=now.tzinfo.key, weekday=self.weekday, states=self.states, pages=pages)
            as_goal()
            return tasks.followups.configure(goal_id, grant, now.timestamp())
        if (self.kind == "inbox" and self.sender_uid
                and re.search(rf"(?<![0-9]){self.sender_uid}(?![0-9])", self.quote)
                and re.search(r"私信|来信|收件", self.quote)):
            as_goal()
            state = tasks.goal_events.get(goal_id)
            grant = EventGrant(source="bilibili_inbox", sender_uid=self.sender_uid, epoch=epoch,
                               revision=state["revision"], after_reply=self.after_reply)
            return tasks.goal_events.configure(goal_id, grant)
        if self.kind == "comment" and self.resource and self.root_id and re.search(r"评论|楼", self.quote) and all(
            coordinate in self.quote for coordinate in (self.resource.removeprefix("dynamic:"), self.root_id)
        ):
            manager = tasks.goal_events.comments
            as_goal()
            grant = CommentWatch(resource=self.resource, root_id=self.root_id, epoch=epoch,
                                 revision=manager.get(goal_id)["revision"])
            return manager.configure(goal_id, grant)
        raise ValueError("请指定要跟进的来信 UID，或视频／动态及评论楼 ID。")


def object_scope(runtime):
    return content_hash(runtime.tasks.scope())


def owner_object(character):
    binding = character.get("bilibili_owner") or {}
    if uid := binding.get("uid"):
        return ConversationObject(kind="owner", identity=str(uid), label="绑定的 B 站主人",
                                  revision=content_hash(binding))
    return None


def binding_matches(subject, character):
    return subject == owner_object(character)


def operating_account_object(facts):
    if uid := facts.get("uid"):
        return ConversationObject(kind="account", identity=uid, label="B 站运营登录账号",
                                  revision=content_hash(facts))
    return None


def owner_binding(character):
    binding = character.get("bilibili_owner") or {}
    if not binding.get("uid"):
        return {"reply_text": "还没有绑定 B 站主人呀。", "card": None}
    uid = str(binding["uid"])
    profile = {"name": binding["name"], "uid": uid, "url": f"https://space.bilibili.com/{uid}"}
    return {"reply_text": f"我绑定的 B 站主人是{binding['name']}，UID 是 {uid}。",
            "card": {"type": "owner_profile", "data": profile},
            "tool_receipt": {"action": "read", "group": "identity", "values": profile}}


_TODAY = re.compile(r"今天|今日|今儿|本日|当天|\btoday\b", re.I)
_WEB_SOURCE = re.compile(r"公开网页|网上|联网|上网|互联网|新闻|头条|外面.{0,6}(?:消息|动态)|https://|\b(?:web|news)\b", re.I)
_LOOKUP = re.compile(r"搜索|搜[一搜下寻索]|搜一下|去搜|检索|查|找|翻|打开|看看|核对|核实|\b(?:search|look up|check)\b", re.I)
_HYPOTHETICAL = re.compile(r"如果|假如|要是|先别|不用|不要|举例")
_DELIVERY = re.compile(r"后台|持续|定时|每天|每周|到时候|报告|整理成|制成|写成|保存|等.+再")
_BACKGROUND = re.compile(r"^(?:请|麻烦|能否|帮我)?(?:交给后台|让后台|安排后台|委托后台)[，、：:\s]*")
_SEARCH_NOISE = re.compile(r"^(?:最新|今天|今日|今儿|近日|新闻|新消息|消息|要[闻聞]|资料|外面|网上|进展|动态)+$")


def requests_public_lookup(message):
    return (bool(_WEB_SOURCE.search(message) and _LOOKUP.search(message))
            and not (_HYPOTHETICAL.search(message) or _DELIVERY.search(message)))


def requests_background_work(message):
    return bool(_BACKGROUND.match(message.strip()) and not _HYPOTHETICAL.search(message))


def search_terms(query, message):
    terms = [term for term in dict.fromkeys(query.terms)
             if term and term in message and not (_SEARCH_NOISE.fullmatch(term) or _LOOKUP.fullmatch(term))]
    localized = query.localized.strip()
    if not terms or not re.fullmatch(r"[\w ・.\-]{2,120}", localized) or _SEARCH_NOISE.fullmatch(localized):
        localized = ""
    return " ".join(terms) or None, localized


async def public_research(runtime, request_id, message, *, topic="", localized=""):
    tasks = runtime.tasks
    scope = tasks.scope()
    local_now = operator_facts.now(runtime.browsing)
    today = local_now.date() if _TODAY.search(message) else None
    # A public query may use only text from the current utterance. Other private
    # context remains available to the conversation, never to the search engine.
    query = (localized if localized and today else topic) or message
    query = query[:140]
    receipts = []

    async def read(name, args):
        try:
            result = await tasks.read(name, request_id, message, scope, args, receipts)
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            result = {"error": "公开来源暂时不可读"}
        receipts.append({"tool": name, "arguments": args, "result": result})
        return result

    addresses = []
    timed_out = False
    try:
        async with asyncio.timeout(10):
            if not tasks.unread_web_addresses(message, ()):
                await read("web_search", {"query": query, **({"news": True, "bulletin": topic is None} if today else {})})
            addresses = tasks.unread_web_addresses(message, receipts)[:4]
            await asyncio.gather(*(read("web_page", {"web_fetch": url}) for url in addresses))
    except TimeoutError:
        timed_out = True
    opened = [step["result"] for step in receipts if step["tool"] == "web_page"]
    sources = {}
    undated = 0
    for page in opened:
        if not page.get("excerpt") or not page.get("url"):
            continue
        value = page.get("published_at")
        if today:
            if not value:
                undated += 1
                continue
            if not published_on(value, today.isoformat(), local_now.tzinfo.key, now=local_now):
                continue
        sources[page["url"]] = {
            "title": page["title"] or httpx.URL(page["url"]).host,
            "entries": [{"kind": "web", "href": page["url"], "text": page["excerpt"],
                         "published_at": value}],
        }
    if sources:
        reply = f"我核对了 {len(sources)} 个{'今天发布的' if today else '公开'}来源。"
        reply += "\n" + "\n".join("· " + group["title"] for group in sources.values())
    elif addresses and today:
        reply = f"我打开了候选网页，目前没有核实到 {today} 发布且可读取的来源。"
        if undated:
            reply += f"其中 {undated} 个可读网页没有可核对的发布日期。"
    elif timed_out:
        reply = "公开来源还在响应，这轮暂时没读到正文。"
    else:
        reply = "这次公开检索没有读到可用来源。"
    # A dated continuation retains the same day filter. Candidate URLs and
    # completed, eligible reads can seed a read-only background task.
    usable = [{"tool": step["tool"], "arguments": step["arguments"], "result": step["result"]}
              for step in receipts if not step["result"].get("error") and (
                  step["tool"] == "web_search" or step["result"].get("url") in sources)]
    return {"reply_text": reply, "card": None, "sources": list(sources.values()),
            "research_continuation": {"reads": usable, "local_date": str(today) if today else None,
                                      "timezone": str(local_now.tzinfo)} if timed_out else None,
            "tool_receipt": {"action": "read", "group": "web",
                             "state": "partial" if timed_out and sources else "unavailable" if not sources else "ok",
                             "local_date": str(today) if today else None,
                             "calls": [{"tool": step["tool"], "status": "unavailable" if step["result"].get("error")
                                        else "ok"} for step in receipts]}}
