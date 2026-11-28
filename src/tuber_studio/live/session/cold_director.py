"""Cold-Room Director: 4-tier idle escalation & cozy companion radio for live sessions."""

from __future__ import annotations

import inspect
import time
from typing import Any

TIER_1_START_SEC = 90.0
TIER_2_START_SEC = 300.0
TIER_3_START_SEC = 900.0

TIER_1_CADENCE_SEC = 90.0
TIER_2_CADENCE_SEC = 150.0
TIER_3_DEFAULT_CADENCE_SEC = 300.0  # within 240..360s

_TIER_LABELS = {
    0: "active_conversation",
    1: "warmup_banter",
    2: "interactive_rotation",
    3: "cozy_companion_radio",
}

_TIER1_LINES: tuple[str, ...] = (
    "都安静啃白饭呢？说说今天最让你想倒回去重看一遍的摸鱼瞬间吧。",
    "弹幕这么乖反而让我有点不习惯……今天遇到什么离谱小事，快讲出来让我乐一下。",
    "趁着这会儿人不挤，点个小话题或者顺手丢个问题过来也行啊，我可还清醒着呢。",
)

_TIER3_DOODLE_LINES: tuple[str, ...] = (
    "你们忙自己的就行，我在这边边随手涂几笔小鲸鱼发呆，BGM开着当背景音也挺舒服。",
    "挂机小涂鸦时间到——本小姐趁没人吵的时候偷偷练两笔简笔画，你们回头别嫌丑。",
)

_TIER3_MUSINGS: tuple[str, ...] = (
    "夜深了或者在忙工作的话就把直播间放着当白噪音吧，顺便想想要不要去盛一碗热白饭。",
    "其实偶尔这样安安静静聊几句公开趣闻也挺好，不急着赶节奏，水杯见底记得去续一杯温水。",
)

_TIER3_MUSIC_MOODS: tuple[str, ...] = (
    "这会儿的节奏正适合放点轻快的夜间电台感旋律，当个不打扰人的桌面小挂件也不错。",
    "今天这份轻松电台氛围我打八十分，剩下的二十分留给一会儿突然冒出来抢沙发的家伙。",
)

_WAKEUP_TIER2_LINES: tuple[str, ...] = (
    "哦？终于有人说话了啊，我还以为弹幕全去集体啃白饭了呢。",
    "哟，总算冒头啦？刚才本小姐自己玩得正起劲呢，既然来了就别再潜水了。",
)

_WAKEUP_TIER3_LINES: tuple[str, ...] = (
    "哎呀，总算有活人回来冒泡了？刚才本小姐电台都放了好几轮了，算你赶上好时候。",
    "哼，可算舍得回神啦？我还以为今天要一路陪你们静音挂机到下播呢，欢迎回来。",
)


async def _maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


class ColdRoomDirector:
    """Manages 0s -> 3600s+ cold-room progression using injected timestamps."""

    def __init__(self, *, now: float | None = None, tier3_cadence: float = TIER_3_DEFAULT_CADENCE_SEC):
        if not (240.0 <= float(tier3_cadence) <= 360.0):
            raise ValueError("tier3_cadence must be between 240 and 360 seconds")
        base_now = time.monotonic() if now is None else float(now)
        self.tier3_cadence = float(tier3_cadence)
        self.last_viewer_activity: float = base_now
        self.last_action_at: float | None = None
        self.last_action_tier: int = 0
        self.tier1_index: int = 0
        self.tier2_index: int = 0
        self.tier3_index: int = 0
        self.total_segments: int = 0
        self.last_segment: dict[str, Any] | None = None

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else float(now)

    def current_tier(self, now: float | None = None) -> int:
        elapsed = max(0.0, self._now(now) - self.last_viewer_activity)
        if elapsed < TIER_1_START_SEC:
            return 0
        if elapsed < TIER_2_START_SEC:
            return 1
        if elapsed < TIER_3_START_SEC:
            return 2
        return 3

    def next_action_due(self, now: float | None = None) -> float | None:
        current = self._now(now)
        tier = self.current_tier(current)
        if tier == 0:
            return self.last_viewer_activity + TIER_1_START_SEC
        if self.last_action_at is None or self.last_action_tier < tier:
            tier_start = {
                1: self.last_viewer_activity + TIER_1_START_SEC,
                2: self.last_viewer_activity + TIER_2_START_SEC,
                3: self.last_viewer_activity + TIER_3_START_SEC,
            }[tier]
            return tier_start
        cadence = {
            1: TIER_1_CADENCE_SEC,
            2: TIER_2_CADENCE_SEC,
            3: self.tier3_cadence,
        }[tier]
        return self.last_action_at + cadence

    def on_viewer_activity(self, now: float | None = None) -> dict[str, Any] | None:
        """Reset idle timer to Tier 0; return one-shot tsundere wakeup cue when recovering from Tier >= 2."""
        current = self._now(now)
        prev_tier = self.current_tier(current)
        self.last_viewer_activity = current
        self.last_action_at = None
        self.last_action_tier = 0

        if prev_tier < 2:
            return None
        lines = _WAKEUP_TIER3_LINES if prev_tier >= 3 else _WAKEUP_TIER2_LINES
        spoken = lines[self.total_segments % len(lines)]
        return {
            "from_tier": prev_tier,
            "spoken_text": spoken,
        }

    def reset(self, now: float | None = None) -> None:
        current = self._now(now)
        self.last_viewer_activity = current
        self.last_action_at = None
        self.last_action_tier = 0
        self.tier1_index = 0
        self.tier2_index = 0
        self.tier3_index = 0
        self.total_segments = 0
        self.last_segment = None

    async def _run_draw_teaser(self, llm: Any, draw_and_guess: Any, *, mode: str = "aqua_draws") -> dict[str, Any] | None:
        if draw_and_guess is None:
            return None
        if hasattr(draw_and_guess, "is_active") and draw_and_guess.is_active():
            return None
        if hasattr(draw_and_guess, "start_round_llm"):
            try:
                return await _maybe_await(
                    draw_and_guess.start_round_llm(llm, trigger_user="Aqua", mode=mode)
                )
            except TypeError:
                return await _maybe_await(draw_and_guess.start_round_llm(llm, trigger_user="Aqua"))
        if hasattr(draw_and_guess, "start_round"):
            return draw_and_guess.start_round(trigger_user="Aqua")
        return None

    async def _run_roast_segment(self, llm: Any, roast_radio: Any, store: Any) -> dict[str, Any] | None:
        if roast_radio is None or not hasattr(roast_radio, "generate_roast_segment"):
            return None
        return await _maybe_await(roast_radio.generate_roast_segment(llm, store))

    async def _run_turtle_teaser(self, llm: Any, turtle_soup: Any) -> dict[str, Any] | None:
        if turtle_soup is None:
            return None
        if hasattr(turtle_soup, "is_active") and turtle_soup.is_active():
            return None
        if hasattr(turtle_soup, "start_round_llm"):
            return await _maybe_await(turtle_soup.start_round_llm(llm, trigger_user="Aqua"))
        if hasattr(turtle_soup, "start_round"):
            return turtle_soup.start_round(trigger_user="Aqua")
        return None

    async def next_segment(
        self,
        *,
        now: float | None = None,
        llm: Any = None,
        draw_and_guess: Any = None,
        turtle_soup: Any = None,
        roast_radio: Any = None,
        store: Any = None,
        options: Any = None,
    ) -> dict[str, Any] | None:
        """Emit the next due cold-room segment for Tier 1..3, or None if not yet due."""
        current = self._now(now)
        tier = self.current_tier(current)
        if tier == 0:
            return None
        due = self.next_action_due(current)
        if due is not None and current < due:
            return None

        segment: dict[str, Any]
        if tier == 1:
            custom_text = (getattr(options, "idle_text", "") or "").strip() if options is not None else ""
            if custom_text and self.tier1_index == 0:
                spoken = custom_text
            else:
                spoken = _TIER1_LINES[self.tier1_index % len(_TIER1_LINES)]
            self.tier1_index += 1
            segment = {
                "tier": 1,
                "kind": "warmup_banter",
                "show": None,
                "spoken_text": spoken,
                "payload": {},
            }

        elif tier == 2:
            slot = self.tier2_index % 3
            self.tier2_index += 1
            if slot == 0:
                payload = await self._run_draw_teaser(llm, draw_and_guess, mode="aqua_draws") or {}
                spoken = payload.get("spoken_text") or "没人说话？那本小姐自己开一局涂鸦，看得懂的随时在弹幕抢答。"
                segment = {
                    "tier": 2,
                    "kind": "show_rotation",
                    "show": "draw_and_guess",
                    "spoken_text": spoken,
                    "payload": payload,
                }
            elif slot == 1:
                payload = await self._run_roast_segment(llm, roast_radio, store) or {}
                spoken = payload.get("spoken_text") or "趁着弹幕在潜水，我翻了翻最近看过的公开视频，这槽不吐不快。"
                segment = {
                    "tier": 2,
                    "kind": "show_rotation",
                    "show": "roast_radio",
                    "spoken_text": spoken,
                    "payload": payload,
                }
            else:
                payload = await self._run_turtle_teaser(llm, turtle_soup) or {}
                spoken = payload.get("spoken_text") or "来锅轻量海龟汤醒醒神吧——听完汤面随时甩一个是非问题过来。"
                segment = {
                    "tier": 2,
                    "kind": "show_rotation",
                    "show": "turtle_soup",
                    "spoken_text": spoken,
                    "payload": payload,
                }

        else:
            slot = self.tier3_index % 3
            self.tier3_index += 1
            if slot == 0:
                payload = await self._run_draw_teaser(llm, draw_and_guess, mode="aqua_draws") or {}
                spoken = _TIER3_DOODLE_LINES[(self.tier3_index - 1) % len(_TIER3_DOODLE_LINES)]
                segment = {
                    "tier": 3,
                    "kind": "companion_radio",
                    "show": "doodle",
                    "spoken_text": spoken,
                    "payload": payload,
                }
            elif slot == 1:
                topic_info = None
                if roast_radio is not None and hasattr(roast_radio, "pick_public_watch_topic"):
                    topic_info = roast_radio.pick_public_watch_topic(store)
                if topic_info:
                    spoken = f"顺手聊个公开小见闻：「{topic_info['topic']}」，就当深夜广播随缘听听啦。"
                else:
                    spoken = _TIER3_MUSINGS[(self.tier3_index - 1) % len(_TIER3_MUSINGS)]
                segment = {
                    "tier": 3,
                    "kind": "companion_radio",
                    "show": "public_musing",
                    "spoken_text": spoken,
                    "payload": topic_info or {},
                }
            else:
                spoken = _TIER3_MUSIC_MOODS[(self.tier3_index - 1) % len(_TIER3_MUSIC_MOODS)]
                segment = {
                    "tier": 3,
                    "kind": "companion_radio",
                    "show": "music_mood",
                    "spoken_text": spoken,
                    "payload": {},
                }

        self.last_action_at = current
        self.last_action_tier = tier
        self.total_segments += 1
        self.last_segment = segment
        return segment

    def status(self, now: float | None = None) -> dict[str, Any]:
        current = self._now(now)
        tier = self.current_tier(current)
        elapsed = max(0.0, current - self.last_viewer_activity)
        due = self.next_action_due(current)
        return {
            "tier": tier,
            "tier_label": _TIER_LABELS[tier],
            "elapsed_seconds": round(elapsed, 1),
            "next_action_due": due,
            "seconds_until_next": max(0.0, round(due - current, 1)) if due is not None else None,
            "total_segments": self.total_segments,
            "last_action_at": self.last_action_at,
            "last_segment_kind": self.last_segment.get("kind") if self.last_segment else None,
            "last_spoken_text": self.last_segment.get("spoken_text") if self.last_segment else None,
        }
