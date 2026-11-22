"""Channel-bound activity, history, and issue projections for operations root callers."""

from .channels.comments.timeline import (
    COMMENT_ID,
    comment_activity,
    comment_href,
    comment_thread,
    resource_key,
)
from .channels.direct_messages.outreach import contact_state
from .kernel.activity import (
    acknowledge_issues,
    clear_issues,
    issue_history,
)
from .kernel.activity import (
    activity as _kernel_activity,
)
from .kernel.activity import (
    history as _kernel_history,
)

ACTIVITY_CHANNELS = {
    "COMMENT_ID": COMMENT_ID,
    "comment_activity": comment_activity,
    "comment_href": comment_href,
    "comment_thread": comment_thread,
    "resource_key": resource_key,
    "contact_state": contact_state,
}


def history(browsing, *, day=None, page=1, page_size=10, visible_since=None):
    """Compatibility view of shared video history aggregation."""
    return _kernel_history(browsing, day=day, page=page, page_size=page_size, visible_since=visible_since)


def activity(
    browsing,
    *,
    day=None,
    kind="all",
    comment_filter="all",
    source="all",
    page=1,
    page_size=10,
    visible_since=None,
    round_id=None,
    paginate=True,
):
    """Unified operator activity view bound to operations channel timeline/outreach helpers."""
    return _kernel_activity(
        browsing,
        day=day,
        kind=kind,
        comment_filter=comment_filter,
        source=source,
        page=page,
        page_size=page_size,
        visible_since=visible_since,
        round_id=round_id,
        paginate=paginate,
        channels=ACTIVITY_CHANNELS,
    )


__all__ = [
    "ACTIVITY_CHANNELS",
    "acknowledge_issues",
    "activity",
    "clear_issues",
    "history",
    "issue_history",
]
