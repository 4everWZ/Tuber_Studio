"""Throttled handoff from Discovery video/comment activity to channel Dynamics."""

import time


class DynamicTrigger:
    """Coordinate optional background dynamic generation without blocking Discovery."""

    def __init__(self, browsing, *, min_interval_seconds: float = 300.0):
        self.browsing = browsing
        self.min_interval_seconds = float(min_interval_seconds)
        self._last_trigger_at = 0.0

    def notify_material_saved(self, row, bvid):
        """Signal channel dynamics when Discovery persists fresh public material."""
        dynamics = getattr(self.browsing, "dynamics", None)
        if dynamics is None or row.get("preview"):
            return False
        now = time.time()
        if now - self._last_trigger_at < self.min_interval_seconds:
            return False
        self._last_trigger_at = now
        changed = getattr(dynamics, "changed", None)
        if changed is not None:
            changed.set()
        return True
