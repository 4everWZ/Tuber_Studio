"""Modular Agent Tools and Capabilities Architecture.

Provides a registry for two kinds of agent tools:
1. DIRECT_ACTION: Deterministic, zero-LLM latency interception (Draw & Guess answer checking, song requests, gift bounties).
2. CONTEXT_AUGMENT: Lightweight, pre-inference context enrichment (historical recall, active anecdotes, game state).
"""

from __future__ import annotations

import enum
import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)


class ToolKind(enum.Enum):
    DIRECT_ACTION = "direct_action"
    CONTEXT_AUGMENT = "context_augment"


class BaseAgentTool(ABC):
    """Abstract base class for all agent capabilities/tools."""

    name: str = "base_tool"
    description: str = ""
    kind: ToolKind = ToolKind.CONTEXT_AUGMENT

    @abstractmethod
    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        """Evaluate whether this tool should activate for the given event and context."""
        ...

    @abstractmethod
    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> Any:
        """Execute the tool logic (e.g. database query, game action, state check)."""
        ...

    def render_context(self, data: Any) -> str | None:
        """Render prompt context string for CONTEXT_AUGMENT tools. Returns None if nothing to inject."""
        if data is None:
            return None
        if isinstance(data, str):
            return data
        return json.dumps(data, ensure_ascii=False)


class RecentAnecdotesTool(BaseAgentTool):
    """Injects high-relevance active community highlights from the last 3 days."""

    name = "recent_anecdotes"
    description = "Provides recent active community highlights and jokes (rolling 3 days) for ambient banter."
    kind = ToolKind.CONTEXT_AUGMENT

    def __init__(self, daily_memory_service):
        self.daily_memory = daily_memory_service

    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        mode = context_info.get("mode", "live")
        return bool(self.daily_memory and context_info.get("daily_memory_enabled", True)
                    and mode in ("live", "comment_draft", "direct_message"))

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> list[dict[str, Any]]:
        character = context_info.get("character", {})
        query = event.get("text", "") if event else ""
        anecdotes = self.daily_memory.get_active_anecdotes(
            character.get("id", "whale"),
            character.get("version", 1),
            query_text=query,
            days=3,
            limit=2,
            max_tokens=150,
            include_incidents=False,
        )
        scope = context_info.get("scope", "")
        try:
            public_floor = (
                json.loads(scope.removeprefix("comments:")) if scope.startswith("comments:") else []
            )
        except (TypeError, ValueError):
            public_floor = []
        if len(public_floor) != 4 or not public_floor[0] or public_floor[1] != "bilibili":
            public_floor = []
        experiences = self.daily_memory.context(
            character,
            query,
            video_id=public_floor[2] if public_floor else None,
            thread_id=public_floor[3] if public_floor else None,
        )
        incident_ids = {int(item["id"].split(":")[1]) for item in experiences}
        return experiences + [item for item in anecdotes if item["id"] not in incident_ids]

    def render_context(self, data: list[dict[str, Any]]) -> str | None:
        if not data:
            return None
        return (
            "近期生活与社群新鲜事："
            + json.dumps(data, ensure_ascii=False)
        )


class HistoricalRecallTool(BaseAgentTool):
    """Passive fallback retrieval for archived or older community highlights when retrospective cues are detected."""

    name = "historical_recall"
    description = "Passively recalls older or archived community highlights (<60 chars) on explicit retrospective questions."
    kind = ToolKind.CONTEXT_AUGMENT

    def __init__(self, daily_memory_service):
        self.daily_memory = daily_memory_service

    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        if not self.daily_memory or not context_info.get("daily_memory_enabled", True):
            return False
        mode = context_info.get("mode", "live")
        if mode not in ("live", "comment_draft", "direct_message"):
            return False
        query = event.get("text", "") if event else ""
        return self.daily_memory.is_retrospective_query(query)

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> dict[str, Any] | None:
        character = context_info.get("character", {})
        query = event.get("text", "") if event else ""
        exclude_ids = context_info.get("active_anecdote_ids", set())
        return self.daily_memory.recall_historical_highlight(
            character.get("id", "whale"),
            character.get("version", 1),
            query,
            exclude_ids=exclude_ids,
            max_chars=65,
        )

    def render_context(self, data: dict[str, Any] | None) -> str | None:
        if not data or not data.get("formatted_clue"):
            return None
        return f"被动唤醒的历史回忆线索：{data['formatted_clue']}"


class GameStatusTool(BaseAgentTool):
    """Injects current autonomous game stream status prompt (<80 tokens)."""

    name = "game_status"
    description = "Injects current active game status (e.g. Stardew Valley, PvZ) into live prompt context."
    kind = ToolKind.CONTEXT_AUGMENT

    def __init__(self, game_livestream_service):
        self.game_livestream = game_livestream_service

    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        mode = context_info.get("mode", "live")
        return bool(self.game_livestream and mode == "live" and self.game_livestream.is_active())

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> str | None:
        return self.game_livestream.get_context_prompt()

    def render_context(self, data: str | None) -> str | None:
        return data


class DrawAndGuessContextTool(BaseAgentTool):
    """Injects active Draw and Guess topic prompt."""

    name = "draw_guess_context"
    description = "Injects Draw and Guess round topic into streamer prompt without revealing answer."
    kind = ToolKind.CONTEXT_AUGMENT

    def __init__(self, draw_and_guess_service):
        self.draw_and_guess = draw_and_guess_service

    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        mode = context_info.get("mode", "live")
        return bool(self.draw_and_guess and mode == "live" and self.draw_and_guess.is_active())

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> dict[str, Any] | None:
        word = self.draw_and_guess.current_word
        return {"category": word["category"]} if word else None

    def render_context(self, data: dict[str, Any] | None) -> str | None:
        if not data:
            return None
        return f"[你画我猜] 你在画画，观众在弹幕竞猜。题目分类：{data['category']}。"


class LiveAmbientEnvironmentTool(BaseAgentTool):
    """Injects real-time current date, time, weekday, and ambient real-world context for live stream."""

    name = "live_ambient_environment"
    description = "Provides real-time date/time, weekday, and ambient context for live stream without polling."
    kind = ToolKind.CONTEXT_AUGMENT

    def __init__(self, live_session=None):
        self.live_session = live_session

    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        return context_info.get("mode") == "live"

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> dict[str, Any]:
        timezone = self.live_session.timezone() if self.live_session else "Asia/Taipei"
        now = datetime.now(ZoneInfo(timezone))
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return {
            "datetime": now.strftime("%Y年%m月%d日 %H:%M"),
            "weekday": f"星期{weekdays[now.weekday()]}",
            "timezone": timezone,
        }

    def render_context(self, data: dict[str, Any] | None) -> str | None:
        if not data:
            return None
        return f"[直播时钟] {data['datetime']}，{data['weekday']}（{data['timezone']}）。"


class LiveBarrageContextTool(BaseAgentTool):
    """Injects recent live room chat context so the streamer perceives audience chatter flow."""

    name = "live_barrage_context"
    description = "Injects recent ambient viewer chatter into live prompt context."
    kind = ToolKind.CONTEXT_AUGMENT

    def __init__(self, live_session=None):
        self.live_session = live_session

    def can_handle(self, event: dict[str, Any], context_info: dict[str, Any]) -> bool:
        return context_info.get("mode") == "live" and self.live_session is not None

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> dict[str, Any]:
        return self.live_session.room_context(event or {}) if self.live_session else {}

    def render_context(self, data: dict[str, Any] | None) -> str | None:
        if not data or not any(data.values()):
            return None
        return (
            "[本场近期弹幕与活动]\n"
            + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        )


class ToolRegistry:
    """Manages agent capabilities and context collection."""

    def __init__(self):
        self._tools: dict[str, BaseAgentTool] = {}

    def register(self, tool: BaseAgentTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseAgentTool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[BaseAgentTool]:
        return list(self._tools.values())

    def collect_context_augmentations(
        self,
        character: dict[str, Any],
        event: dict[str, Any],
        mode: str,
        *,
        extra_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Collect all active CONTEXT_AUGMENT context messages in sequence."""
        context_blocks = []
        ctx_info = {"character": character, "mode": mode, **(extra_context or {})}

        # Track active anecdote IDs to pass to historical recall tool
        active_ids = set()

        for tool in self._tools.values():
            if tool.kind != ToolKind.CONTEXT_AUGMENT:
                continue
            try:
                # Update context_info with discovered active_anecdote_ids
                ctx_info["active_anecdote_ids"] = active_ids

                if tool.can_handle(event, ctx_info):
                    res = tool.execute(event, ctx_info)

                    # If this was RecentAnecdotesTool, collect its IDs
                    if tool.name == "recent_anecdotes" and isinstance(res, list):
                        for item in res:
                            if "id" in item:
                                active_ids.add(item["id"])

                    rendered = tool.render_context(res)
                    if rendered:
                        block = {"role": "user", "kind": "context", "content": rendered}
                        if tool.name in {"recent_anecdotes", "historical_recall"} and ctx_info.get("on_memory"):
                            ctx_info["on_memory"](block)
                        context_blocks.append(block)
            except Exception as e:
                LOGGER.warning("Tool %s execution failed: %s", tool.name, e)

        return context_blocks


def build_default_tool_registry(
    *,
    daily_memory=None,
    game_livestream=None,
    draw_and_guess=None,
    live_session=None,
) -> ToolRegistry:
    """Factory creating and registering standard Tuber Studio tools."""
    registry = ToolRegistry()
    registry.register(LiveAmbientEnvironmentTool(live_session))
    if live_session:
        registry.register(LiveBarrageContextTool(live_session))
    if daily_memory:
        registry.register(RecentAnecdotesTool(daily_memory))
        registry.register(HistoricalRecallTool(daily_memory))
    if game_livestream:
        registry.register(GameStatusTool(game_livestream))
    if draw_and_guess:
        registry.register(DrawAndGuessContextTool(draw_and_guess))
    return registry
