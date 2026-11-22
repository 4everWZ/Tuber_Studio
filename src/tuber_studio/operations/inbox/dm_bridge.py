"""Private direct-message bridge between Pipeline B (`InboxRunner`) and `channels/direct_messages`."""

import time

from ..channels.direct_messages.outreach import handoff_comment
from .inbound import normalize_inbound_event


class DMBridge:
    """Coordinates `channels/direct_messages` inbox polling & privacy-isolated projection for InboxRunner."""

    def __init__(self, browsing):
        self.browsing = browsing

    async def sync_once(self) -> dict:
        """Poll DM inbox & resume pending DM deliveries without crossing character/user isolation."""
        messages = self.browsing.messages
        result = {"polled": 0, "resumed": False}
        policy = messages.policy()
        if not policy.get("enabled"):
            return result
        await messages.resume_pending()
        result["resumed"] = True
        if policy.get("auto_poll"):
            synced = await messages.sync(automatic=True)
            result["polled"] = int(synced.get("imported", 0)) if isinstance(synced, dict) else int(bool(synced))
        return result

    def list_recent(self, *, account_id: str, character_id: str, platform_user_id: str, limit: int = 20) -> list:
        """Return strictly scoped DM messages for `(account_id, character_id, platform_user_id)`."""
        rows = self.browsing.store.all(
            "SELECT * FROM dm_inbox WHERE platform='bilibili' AND account_id=? AND character_id=? "
            "AND platform_user_id=? ORDER BY sent_at DESC, id DESC LIMIT ?",
            (str(account_id), str(character_id), str(platform_user_id), int(limit)),
        )
        return [
            normalize_inbound_event(dict(row), kind_hint="dm", character_id=str(character_id))
            for row in rows
        ]


async def sync_dm_inbox(browsing) -> dict:
    """Convenience entrypoint bridging `InboxRunner` with `channels/direct_messages`."""
    return await DMBridge(browsing).sync_once()


def dm_privacy_scope_key(account_id: str, character_id: str, platform_user_id: str) -> tuple[str, str, str]:
    """Canonical strict isolation key `(account_id, character_id, platform_user_id)` for DM memory."""
    return (str(account_id), str(character_id), str(platform_user_id))


def record_dm_sync_timestamp(browsing) -> float:
    now = time.time()
    browsing.store.set("inbox_last_dm_sync", now)
    return now


__all__ = [
    "DMBridge",
    "dm_privacy_scope_key",
    "handoff_comment",
    "record_dm_sync_timestamp",
    "sync_dm_inbox",
]
