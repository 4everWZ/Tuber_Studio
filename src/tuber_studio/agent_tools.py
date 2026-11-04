"""Modular Agent Tools and Capabilities Architecture.

Provides a decoupled, extensible framework for agent tools across three execution tiers:
1. DIRECT_ACTION: Deterministic, zero-LLM latency interception (Draw & Guess answer checking, song requests, gift bounties).
2. CONTEXT_AUGMENT: Lightweight, pre-inference context enrichment (historical recall, active anecdotes, game state).
3. BACKGROUND_TASK: Asynchronous offline/operations tasks (daily distillation, draft generation).
"""

from __future__ import annotations

import enum
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

LOGGER = logging.getLogger(__name__)


class ToolKind(enum.Enum):
    DIRECT_ACTION = "direct_action"
    CONTEXT_AUGMENT = "context_augment"
    BACKGROUND_TASK = "background_task"


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
        return bool(self.daily_memory and mode in ("live", "comment_draft"))

    def execute(self, event: dict[str, Any], context_info: dict[str, Any]) -> list[dict[str, Any]]:
        character = context_info.get("character", {})
        query = event.get("text", "") if event else ""
        return self.daily_memory.get_active_anecdotes(
            character.get("id", "whale"),
            character.get("version", 1),
            query_text=query,
            days=3,
            limit=2,
            max_tokens=150,
        )

    def render_context(self, data: list[dict[str, Any]]) -> str | None:
        if not data:
            return None
        return (
            "近期生活与社群新鲜事（闲聊或被问及时自然随口提及，不要死板背诵）："
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
        if not self.daily_memory:
            return False
        mode = context_info.get("mode", "live")
        if mode not in ("live", "comment_draft"):
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
        return (
            f"被动唤醒的历史回忆线索（观众主动询问过去时可随口提及，勿主动长篇大论）：{data['formatted_clue']}"
        )


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
        return self.draw_and_guess.current_word

    def render_context(self, data: dict[str, Any] | None) -> str | None:
        if not data or "word" not in data:
            return None
        return (
            f"[当前直播间正在进行【你画我猜】小游戏，你正在画《{data['word']}》（分类: {data['category']}）。"
            "观众在弹幕竞猜，请以俏皮画手口吻互动，但绝不要提前直接说出答案！]"
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
                        context_blocks.append(
                            {
                                "role": "user",
                                "kind": "context",
                                "content": rendered,
                            }
                        )
            except Exception as e:
                LOGGER.warning("Tool %s execution failed: %s", tool.name, e)

        return context_blocks


def build_default_tool_registry(
    *,
    daily_memory=None,
    game_livestream=None,
    draw_and_guess=None,
) -> ToolRegistry:
    """Factory creating and registering standard Tuber Studio tools."""
    registry = ToolRegistry()
    if daily_memory:
        registry.register(RecentAnecdotesTool(daily_memory))
        registry.register(HistoricalRecallTool(daily_memory))
    if game_livestream:
        registry.register(GameStatusTool(game_livestream))
    if draw_and_guess:
        registry.register(DrawAndGuessContextTool(draw_and_guess))
    return registry
