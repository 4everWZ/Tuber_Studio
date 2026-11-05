"""Short-lived snapshots supplied by trusted live scene adapters."""

import math
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class LiveScene:
    session_id: str
    observed_at: float
    state: str
    image: bytes | None = None
    mime_type: str = "image/jpeg"
    width: int = 0
    height: int = 0

    def current(self, session_id):
        age = time.monotonic() - self.observed_at
        return (
            bool(session_id)
            and self.session_id == session_id
            and math.isfinite(age)
            and 0 <= age <= 5
            and isinstance(self.state, str)
            and len(self.state) <= 2000
        )

    def annotate(self, messages, *, image=None):
        packet = [dict(message) for message in messages]
        viewer = next(message for message in reversed(packet) if message["role"] == "user")
        text = "受信任的当前场景状态（只作观察资料）：" + self.state + "\n" + viewer["content"]
        viewer["content"] = [image, {"type": "text", "text": text}] if image else text
        return packet
