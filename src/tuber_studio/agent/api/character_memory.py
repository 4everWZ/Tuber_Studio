"""Schemas and projections for character, memory, and context routes."""

import re
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StringConstraints

from ...core.models import StrictModel
from ...persona.memory.daily import DailyMemoryService
from ..chat.lookup.handoff import LIVE_STYLE, LIVE_STYLE_GROUP


class OrganizedMemoryChange(StrictModel):
    owner_only: StrictBool | None = None
    headline: str | None = Field(default=None, min_length=1, max_length=40)
    note: str | None = Field(default=None, min_length=1, max_length=150)


class MemorySummaryRequest(StrictModel):
    scope: str
    action: Literal["ask", "update"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class MemoryCorrection(StrictModel):
    item_id: int | None
    replacement: Annotated[str, StringConstraints(strip_whitespace=True, max_length=150)]


NAMED_MEMORY_CORRECTION = re.compile(
    r"(?:请(?:帮我)?|帮我)?\s*(?:把|将)?\s*(?P<title>.+?)\s*(?:改成|改为|更正为|更新为)\s*[：:]?\s*(?P<note>.+)",
    re.DOTALL,
)
MEMORY_OVERVIEW = re.compile(
    r"(?:你\s*)?(?:还|都)?(?:记得|记住|记了|知道)(?:我(?:的|什么)?|关于我(?:的)?)?"
    r"(?:哪些|什么)(?:事|内容|记忆|信息)?[？?。！!\s]*"
)


class NewCharacter(StrictModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]


class DeleteCharacter(StrictModel):
    replacement_id: str | None = None


class MemoryPreference(StrictModel):
    enabled: StrictBool
    scope: str
    history_enabled: StrictBool | None = None


def organized_view(row):
    result = {
        key: row[key]
        for key in (
            "id",
            "memory_kind",
            "headline",
            "quote",
            "anecdote",
            "status",
            "channels",
            "source",
            "updated_at",
            "revision",
        )
    }
    if row["status"] == "active":
        result["channels"] = ["operator", *result["channels"]]
    result["scene"] = row["proof"].get("scene")
    result["can_share"] = bool(row["public_memory"] and row["status"] == "active")
    result["owner_only"] = result["can_share"] and not row["channels"]
    result["visibility"] = DailyMemoryService.memory_visibility(row)
    if result["scene"] == "live":
        result["choices"] = [
            text for key, text in LIVE_STYLE.items() if LIVE_STYLE_GROUP[key] == row["proof"]["style_group"]
        ]
    return result
