"""Discovery publication package wiring channel helpers into kernel IntentPublisher."""

from .publisher import (
    PUBLICATION_CHANNELS,
    BrowsingPublication,
    DiscoveryPublisher,
    queue_discovery_send,
)
from .receipt_sync import (
    background_publications_pending,
    reconcile_round_publications,
)
from .verification import decode_intent_target, target_matches_scope, verify_video_target

__all__ = [
    "BrowsingPublication",
    "DiscoveryPublisher",
    "PUBLICATION_CHANNELS",
    "background_publications_pending",
    "decode_intent_target",
    "queue_discovery_send",
    "reconcile_round_publications",
    "target_matches_scope",
    "verify_video_target",
]
