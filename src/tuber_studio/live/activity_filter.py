"""Live room activity and poll filtering with structured event synthesis."""

import re
import time
from typing import Any


class LiveActivityFilter:
    """Filters noisy poll/vote and platform activity barrages, synthesizing summary events."""

    def __init__(self):
        self.active_poll: dict[str, Any] | None = None
        self._vote_pattern = re.compile(r"^\s*([1-9]|[A-Za-z]|#\S+)\s*$")
        self._platform_activity_patterns = (
            "进入直播间",
            "关注了主播",
            "分享了直播间",
            "点亮了粉丝勋章",
            "加入了大航海",
            "为主播点赞了",
        )

    def is_poll_active(self) -> bool:
        if not self.active_poll:
            return False
        if time.time() > self.active_poll["expires_at"]:
            return False
        return True

    def start_poll(self, topic: str, options: list[str], duration_seconds: float = 60.0) -> dict[str, Any]:
        """Start a new interactive poll."""
        now = time.time()
        options_map = {}
        for idx, opt in enumerate(options, 1):
            options_map[str(idx)] = opt.strip()
            options_map[chr(ord("A") + idx - 1)] = opt.strip()
            options_map[opt.strip().lower()] = opt.strip()

        self.active_poll = {
            "topic": topic.strip(),
            "options": [opt.strip() for opt in options],
            "options_map": options_map,
            "votes": {},  # user_id -> option_name
            "started_at": now,
            "expires_at": now + duration_seconds,
            "duration": duration_seconds,
        }
        return {
            "topic": topic,
            "options": self.active_poll["options"],
            "started_at": now,
            "duration": duration_seconds,
        }

    def end_poll(self) -> dict[str, Any] | None:
        """Conclude active poll and synthesize a summary result event."""
        if not self.active_poll:
            return None
        poll = self.active_poll
        self.active_poll = None

        counts = {opt: 0 for opt in poll["options"]}
        for voted in poll["votes"].values():
            if voted in counts:
                counts[voted] += 1

        total_votes = len(poll["votes"])
        winner = max(counts.items(), key=lambda kv: kv[1])[0] if counts and total_votes > 0 else "无"

        summary_parts = [f"【{opt}】{counts[opt]}票" for opt in poll["options"]]
        summary_str = "，".join(summary_parts)
        spoken_text = f"【直播间投票结果】《{poll['topic']}》投票已结束！总投票数: {total_votes}。{summary_str}。最终获胜选项是【{winner}】！"

        return {
            "topic": poll["topic"],
            "total_votes": total_votes,
            "counts": counts,
            "winner": winner,
            "summary_text": spoken_text,
        }

    def check_event(self, event: Any) -> tuple[bool, str | None]:
        """Evaluate if an event should be filtered from LLM reply queue.
        Returns: (should_filter, reason_or_none)
        """
        text = getattr(event, "text", "") or ""
        text_clean = text.strip()

        # 1. Platform system broadcast filter
        if any(pat in text_clean for pat in self._platform_activity_patterns):
            return True, "平台系统广播/进房提示，免入回复槽位"

        # 2. Active poll vote tallying and filter
        if self.is_poll_active():
            opt_map = self.active_poll["options_map"]
            voted_opt = None
            if text_clean.upper() in opt_map:
                voted_opt = opt_map[text_clean.upper()]
            elif text_clean.lower() in opt_map:
                voted_opt = opt_map[text_clean.lower()]
            elif self._vote_pattern.match(text_clean):
                match = self._vote_pattern.match(text_clean).group(1).upper()
                if match in opt_map:
                    voted_opt = opt_map[match]

            if voted_opt:
                user_id = getattr(event, "platform_user_id", "") or "anonymous"
                self.active_poll["votes"][str(user_id)] = voted_opt
                return True, f"投票选项【{voted_opt}】已计票，不占用对话槽位"

        return False, None
