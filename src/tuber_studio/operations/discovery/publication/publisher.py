"""Channel-bound IntentPublisher and Discovery background send helpers."""

from ...channels.attachments.attachments import (
    TEXT_ONLY_IMAGE,
    checked_image,
    comment_attachment_allowed,
    comment_image,
    comment_payload,
    decision,
    image_choice,
    image_scope,
    queue_image,
    rebind_image,
    record_upload,
    require_comment_attachment,
    row_ref,
    wait_decision,
)
from ...channels.comments.materials import (
    model_comment_materials,
    validate_comment_material_sources,
)
from ...channels.comments.timeline import (
    comment_source,
    comment_thread_context,
    observe_thread,
    operator_reply,
    read_scoped_thread,
    resource_key,
)
from ...channels.mentions import rebase_mentions, revision_payload
from ...kernel.publication import IntentPublisher
from .receipt_sync import reconcile_intent
from .verification import (
    has_legacy_binding,
    intent_publication_binding,
    like_observation,
    liked_target,
    record_account_reply,
    refresh_intent_identity,
    verify_dynamic_like,
    verify_intent_source_before_publish,
    verify_reply_source,
    verify_top_level_target_for_send,
)

PUBLICATION_CHANNELS = {
    "TEXT_ONLY_IMAGE": TEXT_ONLY_IMAGE,
    "checked_image": checked_image,
    "comment_attachment_allowed": comment_attachment_allowed,
    "comment_image": comment_image,
    "comment_payload": comment_payload,
    "comment_source": comment_source,
    "comment_thread_context": comment_thread_context,
    "decision": decision,
    "image_choice": image_choice,
    "image_scope": image_scope,
    "model_comment_materials": model_comment_materials,
    "observe_thread": observe_thread,
    "operator_reply": operator_reply,
    "queue_image": queue_image,
    "read_scoped_thread": read_scoped_thread,
    "rebase_mentions": rebase_mentions,
    "rebind_image": rebind_image,
    "record_upload": record_upload,
    "require_comment_attachment": require_comment_attachment,
    "resource_key": resource_key,
    "revision_payload": revision_payload,
    "row_ref": row_ref,
    "validate_comment_material_sources": validate_comment_material_sources,
    "wait_decision": wait_decision,
}


class DiscoveryPublisher(IntentPublisher):
    """IntentPublisher pre-bound to operations channel collaborators."""

    def __init__(self, browsing, *, channels=None):
        super().__init__(
            browsing,
            channels=channels or getattr(browsing, "publication_channels", None) or PUBLICATION_CHANNELS,
        )

    def _publication_binding(self, row, character, metadata):
        return intent_publication_binding(self, row, character, metadata)

    def _has_legacy_binding(self, intent):
        return has_legacy_binding(self, intent)

    async def _refresh_intent_identity(self, identity, automatic):
        return await refresh_intent_identity(self, identity, automatic)

    async def _reconcile(self, identity):
        return await reconcile_intent(self, identity)

    async def _verify_reply_source(self, target, account):
        return await verify_reply_source(self, target, account)

    async def _verify_dynamic_like(self, target):
        return await verify_dynamic_like(self, target)

    async def _verify_top_level_target(self, row, materials, *, job=None):
        return await verify_top_level_target_for_send(self.browsing, row, materials, job=job)

    async def _verify_intent_source(self, row, account, materials, *, job=None):
        return await verify_intent_source_before_publish(self, row, account, materials, job=job)

    @staticmethod
    def _liked_target(thread, target):
        return liked_target(thread, target)

    @staticmethod
    def _like_observation(target):
        return like_observation(target)

    def _record_account_reply(self, row, handled):
        return record_account_reply(self, row, handled)


def queue_discovery_send(browsing, intent_id: str) -> None:
    """Enqueue an automatic Discovery intent with `direct=False` so direct reserve applies."""
    browsing.sends.enqueue("browsing", intent_id, direct=False)


BrowsingPublication = DiscoveryPublisher
