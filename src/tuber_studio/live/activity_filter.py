"""Room activities are model context, never individual speech jobs."""

import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from ..core.models import StrictModel

MAX_VOTERS = 5000
MAX_VOTE_EVENTS = 20000

_DEFAULT_OPTION_ALIASES: dict[str, tuple[str, ...]] = {
    "你画我猜": ("画猜", "涂鸦", "画画"),
    "灵魂海龟汤": ("海龟汤", "喝汤", "推理"),
    "锐评辩论": ("锐评", "辩论", "吐槽"),
    "点歌听歌会": ("点歌", "听歌", "唱歌", "歌会"),
    "星露谷物语": ("星露谷", "种田", "stardew"),
    "植物大战僵尸": ("pvz", "僵尸", "塔防"),
    "继续闲聊": ("继续聊", "闲聊", "聊天", "继续"),
}


class LivePoll(StrictModel):
    platform: str = Field(default="mock", pattern=r"^(?:\*|[a-z0-9_-]{1,32})$")
    room_id: str = Field(default="debug", min_length=1, max_length=80)
    topic: str = Field(min_length=1, max_length=80)
    options: list[str] = Field(min_length=2, max_length=9)
    duration_seconds: float = Field(default=60, ge=1, le=600)
    aliases: dict[str, str] = Field(default_factory=dict)
    fallback_label: str | None = Field(default=None, max_length=80)

    @field_validator("topic")
    @classmethod
    def clean_topic(cls, value):
        if not value.strip():
            raise ValueError("需要投票主题")
        return value.strip()

    @field_validator("options")
    @classmethod
    def distinct_options(cls, values):
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 40 for value in cleaned):
            raise ValueError("每个选项须为1到40字")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("投票选项不能重复")
        return cleaned


class ActivitySummary(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    phase: Literal["started", "ended"]
    topic: str = Field(min_length=1, max_length=80)
    result: str | None = Field(default=None, max_length=240)


class LiveActivityFilter:
    def __init__(self):
        self.active_poll = None
        self.vote_events = {}

    @property
    def deadline(self):
        return self.active_poll["deadline"] if self.active_poll else None

    def is_poll_active(self):
        return self.deadline is not None and time.monotonic() < self.deadline

    def start_poll(self, request: LivePoll):
        if self.active_poll is not None:
            raise ValueError("当前已有投票，请先结束")
        started_at = datetime.now(UTC).timestamp()
        aliases = {option.casefold(): option for option in request.options}
        for index, option in enumerate(request.options):
            for alias in (str(index + 1), chr(ord("a") + index)):
                aliases.setdefault(alias, option)
            for prefix, extra_aliases in _DEFAULT_OPTION_ALIASES.items():
                if option == prefix or option.startswith(prefix):
                    aliases.setdefault(prefix.casefold(), option)
                    for extra in extra_aliases:
                        aliases.setdefault(extra.casefold(), option)
        valid_options = set(request.options)
        for raw_alias, target_opt in request.aliases.items():
            cleaned_alias = str(raw_alias).strip().casefold()
            if cleaned_alias and target_opt in valid_options:
                aliases.setdefault(cleaned_alias, target_opt)
        public = {
            "id": str(uuid4()),
            "platform": request.platform,
            "room_id": request.room_id,
            "topic": request.topic,
            "options": request.options,
            "started_at": started_at,
            "duration": request.duration_seconds,
            "phase": "started",
        }
        if request.fallback_label:
            public["fallback_label"] = request.fallback_label
        self.active_poll = {
            "public": public,
            "deadline": time.monotonic() + request.duration_seconds,
            "aliases": aliases,
            "voters": {},
            "event_count": 0,
            "saturated": False,
        }
        return public

    def public_poll(self, now: float | None = None) -> dict | None:
        """Return live public poll counts and remaining seconds, or None if inactive/expired."""
        poll = self.active_poll
        if poll is None:
            return None
        public = poll["public"]
        if now is None:
            remaining = poll["deadline"] - time.monotonic()
        elif now > 1_000_000_000:
            remaining = (public["started_at"] + public["duration"]) - float(now)
        else:
            remaining = poll["deadline"] - float(now)
        if remaining <= 0:
            return None
        counts = dict.fromkeys(public["options"], 0)
        for _, voted in poll["voters"].values():
            if voted in counts:
                counts[voted] += 1
        return public | {
            "counts": counts,
            "total_votes": len(poll["voters"]),
            "remaining_seconds": round(max(0.0, remaining), 1),
        }

    def end_poll(self):
        poll, self.active_poll = self.active_poll, None
        if poll is None:
            return None
        public = poll["public"]
        counts = dict.fromkeys(public["options"], 0)
        for _, voted in poll["voters"].values():
            counts[voted] += 1
        total = len(poll["voters"])
        winners = [option for option, count in counts.items() if total and count == max(counts.values())]
        return public | {
            "phase": "ended",
            "total_votes": total,
            "counts": counts,
            "winners": winners,
            "winner": winners[0] if len(winners) == 1 and not poll["saturated"] else None,
            "complete": not poll["saturated"],
        }

    def check_event(self, event):
        """Return a receipt only for typed activity or an exact in-poll ballot."""
        if event.type == "activity":
            return "filtered", "活动参与明细"
        key = (event.platform, event.room_id, event.event_id)
        if key in self.vote_events:
            return "duplicate", "重复投票事件"
        if event.type != "chat" or not self.is_poll_active():
            return None
        poll = self.active_poll
        public = poll["public"]
        platform_ok = public["platform"] == "*" or event.platform == public["platform"]
        room_ok = public["room_id"] == "*" or event.room_id == public["room_id"]
        if not (platform_ok and room_ok):
            return None
        text = event.text.strip().casefold()
        choice = poll["aliases"].get(text) or poll["aliases"].get(text.removeprefix("#"))
        if choice is None:
            return None
        at = event.timestamp.timestamp()
        uid = event.platform_user_id
        if at < public["started_at"] or at > public["started_at"] + public["duration"]:
            return "filtered", "投票不在活动时段"
        if not uid:
            return "filtered", "匿名投票未计票"
        voters = poll["voters"]
        if poll["event_count"] >= MAX_VOTE_EVENTS or (uid not in voters and len(voters) >= MAX_VOTERS):
            poll["saturated"] = True
            return "filtered", "本地投票达到容量上限"
        poll["event_count"] += 1
        self.vote_events[key] = None
        if len(self.vote_events) > MAX_VOTE_EVENTS:
            self.vote_events.pop(next(iter(self.vote_events)))
        if uid not in voters or at >= voters[uid][0]:
            voters[uid] = (at, choice)
        return "filtered", "已计票"
