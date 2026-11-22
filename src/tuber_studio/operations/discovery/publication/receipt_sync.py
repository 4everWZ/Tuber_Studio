"""Receipt reconciliation and background publication pending probes for Discovery."""

import time

from ....core.store import dump
from ...kernel.feedback import FeedbackPublication


async def reconcile_intent(publisher, identity):
    """Run read-only receipt reconciliation for an uncertain browsing intent."""
    comment_payload = publisher._channel("comment_payload")
    feedback = FeedbackPublication(publisher.browsing.runtime, publisher.browsing.store, publisher.browsing.operations)
    if not feedback.coordinator.find("browsing", identity):
        intent = publisher.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        if intent and intent["status"] == "publish_uncertain" and publisher._has_legacy_binding(intent):
            saved = publisher.browsing.store.one(
                """SELECT token FROM operation_write_attempts WHERE account_id=? AND character_id=?
                   AND intent=? AND resolved_status='uncertain' ORDER BY started_at DESC LIMIT 1""",
                (intent["account_id"], intent["character_id"], intent["source_key"]),
            )
            if saved:
                attempt = publisher.browsing.operations.get_attempt(saved["token"])
                source = ((attempt or {}).get("binding") or {}).get("source")
                if isinstance(source, dict) and source.get("local_id") == identity:
                    feedback.record_unknown(
                        "browsing",
                        identity,
                        attempt["token"],
                        attempt["binding"],
                        comment_payload(publisher.browsing.repo.decode(intent), publisher.browsing.runtime, "browsing"),
                        "恢复发送回执核对",
                    )

    async def read(row, attempt):
        intent_row = publisher.browsing.repo.decode(
            publisher.browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,))
        )
        if intent_row and intent_row.get("kind") in ("video_like", "video_fav"):
            return {"status": "unsupported"}
        try:
            await publisher.browsing._current_account(row["binding"]["account_id"])
        except ValueError:
            return {"status": "check_failed", "reason": "account_unavailable"}
        if intent_row and intent_row["kind"] == "comment_like":
            target = intent_row["target"]
            try:
                source = await publisher._verify_reply_source(target, intent_row["account_id"])
            except ValueError:
                return {"status": "check_failed"}
            if publisher._liked_target(source, target):
                return {"status": "visible", "receipt": publisher._like_observation(target)}
            return {"status": "not_found"}
        if intent_row and intent_row["kind"] == "dynamic_like":
            try:
                observed = await publisher._verify_dynamic_like(intent_row["target"])
            except ValueError:
                return {"status": "check_failed"}
            return {"status": "visible", "receipt": observed} if observed else {"status": "not_found"}
        if (
            intent_row
            and comment_payload(intent_row, publisher.browsing.runtime, "browsing").get("asset_sha256")
            and not (attempt["receipt"] or {}).get("rpid")
        ):
            return {"status": "unsupported", "reason": "image_receipt_required"}
        if not hasattr(publisher.browsing.adapter, "reconcile_publication"):
            return {"status": "unsupported"}
        return await publisher.browsing.adapter.reconcile_publication(
            row["binding"]["target"] | {"mentions": row["feedback"]["sent_payload"].get("mentions", [])},
            author_uid=row["binding"]["account_id"],
            text=row["feedback"]["sent_payload"]["text"],
            started_at=attempt["started_at"],
            receipt=attempt["receipt"],
        )

    def commit(receipt, verification):
        publisher.browsing.store.execute(
            "UPDATE browsing_intents SET status='published',receipt=?,visibility=?,error=NULL,updated_at=? WHERE id=?",
            (dump(receipt), verification["status"], time.time(), identity),
        )

    return await feedback.reconcile("browsing", identity, read, commit)


async def reconcile_round_publications(browsing, row, bvid):
    """Reconcile any `publish_uncertain` intents created for `(row['id'], bvid)` in read-only mode."""
    items = [
        item
        for item in browsing.store.all(
            "SELECT * FROM browsing_intents WHERE round_id=? AND status='publish_uncertain'",
            (row["id"],),
        )
        if bvid in item["source_key"]
    ]
    for item in items:
        await browsing.publication._reconcile(item["id"])
    return len(items)


def background_publications_pending(browsing, scope) -> bool:
    """Return True when prior Discovery background sends or handoffs still await settlement."""
    now = time.time()
    for item in browsing.sends.due_background(browsing.operation_scope(), browsing.operations.policy()["epoch"], now):
        draft = browsing._send_draft(item)
        if draft and draft["status"] in {"pending_review", "publishing"}:
            return True
    return browsing.repo.batches.pending_publication_preparation(
        scope,
        now,
        process_handoffs=browsing.messages._processing_authorized(),
    )
