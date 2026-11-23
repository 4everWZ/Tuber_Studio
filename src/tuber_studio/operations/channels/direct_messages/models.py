"""Typed schemas and domain errors for direct-message inbox and conversation windows."""

from dataclasses import dataclass

from pydantic import Field, StrictInt

from ....core.models import ReflectionDirection, StrictModel


class MessagePolicy(StrictModel):
    enabled: bool = False
    auto_poll: bool = False
    auto_send: bool = False
    allow_comment_handoff: bool = True
    channel_max_tokens: int = Field(default=2048, ge=256, le=65536)
    poll_interval_seconds: int = Field(default=60, ge=20, le=86400)
    max_per_hour: int = Field(default=5, ge=0)
    max_per_day: int = Field(default=20, ge=0)
    recipient_cooldown_seconds: int = Field(default=120, ge=10, le=86400)


class IncomingMessage(StrictModel):
    platform: str = Field(default="bilibili", pattern=r"^[a-z0-9_-]{1,32}$")
    message_id: str = Field(min_length=1, max_length=100)
    platform_user_id: str = Field(min_length=1, max_length=100)
    display_name: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    timestamp: float = Field(ge=0)
    account_id: str = Field(default="", max_length=100)


class MessageInputChanged(ValueError):
    """New incoming messages superseded a draft before submission."""


class MessageIdentityChanged(MessageInputChanged):
    """The account profile changed after the latest pre-send refresh."""


class IncomingSettlement(StrictModel):
    incoming: StrictInt
    outgoing: StrictInt
    quote: str = Field(min_length=1, max_length=300)


class ConversationReply(StrictModel):
    text: str = ""
    reason: str = Field(min_length=1, max_length=1024)
    reply_to: list[StrictInt] = Field(default_factory=list, max_length=8)
    handled: list[IncomingSettlement] = Field(default_factory=list, max_length=8)
    public_reflection: ReflectionDirection | None = None


@dataclass(frozen=True)
class ConversationWindow:
    timeline: list[dict]
    refs: dict[int, int]
    links: dict[tuple[int, int], str | None]
    fingerprint: str
    latest_outgoing_id: str | None = None
    unanswered_refs: dict[int, int] = None  # type: ignore[assignment]
    prior_settled: list[tuple[int, str]] = ()  # type: ignore[assignment]
    ambiguous: bool = False
