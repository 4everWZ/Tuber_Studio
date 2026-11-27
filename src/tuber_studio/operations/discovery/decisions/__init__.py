"""Discovery decision coordinator composing shared kernel ResponseEngine with Discovery evaluators."""

from ...channels.comments.materials import model_comment_materials
from ...channels.comments.timeline import comment_thread_context, confirmed_bot_comment_ids, resource_key
from ...channels.direct_messages.outreach import confirmed_invitation
from ...channels.mentions import MENTION_INSTRUCTION, bind_mentions, mention_participants
from ...kernel.decision_engine import ResponseEngine
from ...kernel.generation import generate_with_recovery
from .comment_decision import CommentDecisionMixin
from .dynamic_trigger import DynamicTrigger
from .explanation import decision_explanation, normalized
from .video_decision import VideoDecisionMixin

DECISION_CHANNELS = {
    "resource_key": resource_key,
    "comment_thread_context": comment_thread_context,
    "confirmed_bot_comment_ids": confirmed_bot_comment_ids,
    "confirmed_invitation": confirmed_invitation,
    "model_comment_materials": model_comment_materials,
    "bind_mentions": bind_mentions,
    "mention_participants": mention_participants,
    "mention_instruction": MENTION_INSTRUCTION,
}


class DiscoveryDecisions(VideoDecisionMixin, CommentDecisionMixin, ResponseEngine):
    """Evidence-bound Discovery decision engine with optional Dynamics trigger."""

    def __init__(self, browsing, *, channels=None, dynamic_trigger=None):
        super().__init__(browsing, channels=channels or getattr(browsing, "decision_channels", None) or DECISION_CHANNELS)
        self.dynamic_trigger = dynamic_trigger if dynamic_trigger is not None else DynamicTrigger(browsing)


BrowsingDecisions = DiscoveryDecisions

__all__ = [
    "BrowsingDecisions",
    "DECISION_CHANNELS",
    "DiscoveryDecisions",
    "decision_explanation",
    "generate_with_recovery",
    "normalized",
]
