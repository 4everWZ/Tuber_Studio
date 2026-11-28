"""Transparent comedic Overtime Theater ('白饭/加班能量条') for live sessions."""

from __future__ import annotations

import time
from typing import Any

_GIFT_EXTEND_LINES = (
    "{name} 刚丢了一发【{gift}】把加班能量条怼满了！行吧，本鲸被迫再加 {step} 分钟，这碗白饭算你们赊账的。",
    "喂喂，{name} 这波是铁了心不让本鲸按时干饭啊？好吧好吧，再陪你们 {step} 分钟，仅此一次！",
    "能量条又爆了！你们这帮家伙嘴上说着让本鲸休息，手倒是很诚实嘛——加时 {step} 分钟，不准中途跑路！",
)

_CHAT_EXTEND_LINES = (
    "弹幕聊太嗨把白饭能量条顶满了！算了，看在 {name} 你们这么起劲的份上，本鲸勉为其难再加 {step} 分钟。",
    "被你们聊得差点忘了要下班！加班条满了，那就再续 {step} 分钟，别冷场啊！",
)

_CAPPED_LINES = (
    "能量条满归满，今天已经加班到上限 {max_ot} 分钟了！真得去盛白饭了，下场再继续！",
)

_CLOSING_SOON_LINE = "还有不到五分钟本鲸就要打卡下班干白饭了，没别的本事别想留我！"
_ENDED_LINE = "到点打卡下班！今天的白饭我先干为敬，明天再战！"


class OvertimeTheater:
    """Self-aware tsundere overtime meter balancing 'going for rice' vs audience-fueled extensions."""

    def __init__(
        self,
        *,
        threshold: float = 100.0,
        chat_cooldown_seconds: float = 15.0,
        decay_per_minute: float = 4.0,
        gift_coin_multiplier: float = 1.5,
        chat_base_energy: float = 6.0,
    ) -> None:
        self.threshold = max(10.0, float(threshold))
        self.chat_cooldown_seconds = max(0.0, float(chat_cooldown_seconds))
        self.decay_per_minute = max(0.0, float(decay_per_minute))
        self.gift_coin_multiplier = max(0.1, float(gift_coin_multiplier))
        self.chat_base_energy = max(0.5, float(chat_base_energy))

        self.active: bool = False
        self.energy: float = 0.0
        self.default_base_duration_minutes: int = 60
        self.default_max_overtime_minutes: int = 120
        self.default_extension_step_minutes: int = 15
        self.base_duration_minutes: int = 60
        self.max_overtime_minutes: int = 120
        self.extension_step_minutes: int = 15
        self.overtime_minutes: int = 0
        self.extensions_count: int = 0
        self.started_at: float | None = None
        self.base_end_at: float | None = None
        self.scheduled_end_at: float | None = None
        self.last_tick_at: float | None = None
        self.stage: str = "idle"
        self.last_event_line: str = ""
        self._last_chat_by_user: dict[str, float] = {}
        self._line_index: int = 0
        self._closing_cue_emitted: bool = False
        self._ended_cue_emitted: bool = False

    def config(self) -> dict[str, float | int]:
        return {
            "threshold": self.threshold,
            "gift_coin_multiplier": self.gift_coin_multiplier,
            "chat_base_energy": self.chat_base_energy,
            "chat_cooldown_seconds": self.chat_cooldown_seconds,
            "decay_per_minute": self.decay_per_minute,
            "default_base_duration_minutes": self.default_base_duration_minutes,
            "default_max_overtime_minutes": self.default_max_overtime_minutes,
            "default_extension_step_minutes": self.default_extension_step_minutes,
        }

    def configure(self, patch: dict[str, Any]) -> dict[str, float | int]:
        if not isinstance(patch, dict):
            raise ValueError("overtime 配置必须为字典")
        threshold = float(patch.get("threshold", self.threshold))
        gift_mult = float(patch.get("gift_coin_multiplier", self.gift_coin_multiplier))
        chat_energy = float(patch.get("chat_base_energy", self.chat_base_energy))
        cooldown = float(patch.get("chat_cooldown_seconds", self.chat_cooldown_seconds))
        decay = float(patch.get("decay_per_minute", self.decay_per_minute))
        base_dur = int(patch.get("default_base_duration_minutes", self.default_base_duration_minutes))
        max_ot = int(patch.get("default_max_overtime_minutes", self.default_max_overtime_minutes))
        step = int(patch.get("default_extension_step_minutes", self.default_extension_step_minutes))
        if not (10.0 <= threshold <= 10000.0):
            raise ValueError("加班能量条阈值须在 10..10000 之间")
        if not (0.1 <= gift_mult <= 100.0):
            raise ValueError("礼物能量倍率须在 0.1..100 之间")
        if not (0.5 <= chat_energy <= 50.0):
            raise ValueError("弹幕基础充能须在 0.5..50 之间")
        if not (0.0 <= cooldown <= 600.0):
            raise ValueError("弹幕冷却时间须在 0..600 秒之间")
        if not (0.0 <= decay <= 100.0):
            raise ValueError("每分钟衰减须在 0..100 之间")
        if not (1 <= base_dur <= 1440) or not (0 <= max_ot <= 720) or not (1 <= step <= 120):
            raise ValueError("默认基础/最大/步长时长超出范围")
        self.threshold = threshold
        self.gift_coin_multiplier = gift_mult
        self.chat_base_energy = chat_energy
        self.chat_cooldown_seconds = cooldown
        self.decay_per_minute = decay
        self.default_base_duration_minutes = base_dur
        self.default_max_overtime_minutes = max_ot
        self.default_extension_step_minutes = step
        if not self.active:
            self.base_duration_minutes = base_dur
            self.max_overtime_minutes = max_ot
            self.extension_step_minutes = step
        return self.config()

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    def start_window(
        self,
        *,
        base_duration_minutes: int | None = None,
        max_overtime_minutes: int | None = None,
        extension_step_minutes: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Start an Overtime Theater broadcast window."""
        base_dur = int(self.default_base_duration_minutes if base_duration_minutes is None else base_duration_minutes)
        max_ot = int(self.default_max_overtime_minutes if max_overtime_minutes is None else max_overtime_minutes)
        step = int(self.default_extension_step_minutes if extension_step_minutes is None else extension_step_minutes)
        if base_dur < 1 or base_dur > 1440:
            raise ValueError("基础直播时长须在 1 到 1440 分钟之间")
        if max_ot < 0 or max_ot > 720:
            raise ValueError("最大加班时长须在 0 到 720 分钟之间")
        if step < 1 or step > 120:
            raise ValueError("单次加班步长须在 1 到 120 分钟之间")

        now_ts = self._now(now)
        self.active = True
        self.energy = 0.0
        self.base_duration_minutes = base_dur
        self.max_overtime_minutes = max_ot
        self.extension_step_minutes = step
        self.overtime_minutes = 0
        self.extensions_count = 0
        self.started_at = now_ts
        self.base_end_at = now_ts + base_dur * 60
        self.scheduled_end_at = self.base_end_at
        self.last_tick_at = now_ts
        self.stage = "working"
        self.last_event_line = "打卡开播！今天到点就准时下班干白饭，谁也别想拦本鲸。"
        self._last_chat_by_user.clear()
        self._line_index = 0
        self._closing_cue_emitted = False
        self._ended_cue_emitted = False
        return self.status(now=now_ts)

    def _next_line(self, pool: tuple[str, ...], **kwargs: Any) -> str:
        template = pool[self._line_index % len(pool)]
        self._line_index += 1
        return template.format(**kwargs)

    def _apply_energy(
        self,
        gain: float,
        *,
        source: str,
        display_name: str,
        gift_name: str = "",
        now_ts: float,
    ) -> dict[str, Any]:
        self.energy = max(0.0, self.energy + gain)
        if self.overtime_minutes >= self.max_overtime_minutes:
            self.energy = min(self.threshold, self.energy)
            line = self._next_line(_CAPPED_LINES, max_ot=self.max_overtime_minutes)
            self.last_event_line = line
            return {
                "kind": "overtime_capped",
                "extended": False,
                "capped": True,
                "added_minutes": 0,
                "overtime_minutes": self.overtime_minutes,
                "extensions_count": self.extensions_count,
                "spoken_text": line,
                "trigger_user": display_name,
                "status": self.status(now=now_ts),
            }

        added_total = 0
        while (
            self.energy >= self.threshold
            and self.overtime_minutes < self.max_overtime_minutes
            and self.scheduled_end_at is not None
        ):
            step = min(self.extension_step_minutes, self.max_overtime_minutes - self.overtime_minutes)
            self.energy -= self.threshold
            self.overtime_minutes += step
            self.extensions_count += 1
            self.scheduled_end_at += step * 60
            added_total += step

        if added_total > 0:
            self._closing_cue_emitted = False
            self.stage = "overtime"
            if source == "gift":
                line = self._next_line(
                    _GIFT_EXTEND_LINES,
                    name=display_name or "热心观众",
                    gift=gift_name or "小礼物",
                    step=added_total,
                )
            else:
                line = self._next_line(
                    _CHAT_EXTEND_LINES,
                    name=display_name or "大家",
                    step=added_total,
                )
            self.last_event_line = line
            return {
                "kind": "overtime_extended",
                "extended": True,
                "capped": False,
                "added_minutes": added_total,
                "overtime_minutes": self.overtime_minutes,
                "extensions_count": self.extensions_count,
                "spoken_text": line,
                "trigger_user": display_name,
                "gift_name": gift_name or None,
                "status": self.status(now=now_ts),
            }

        if self.energy >= self.threshold * 0.75:
            self.last_event_line = "警告警告，白饭锅盖快按不住了，再聊再喂就要被迫加班了啊！"
        return {
            "kind": "overtime_energy",
            "extended": False,
            "capped": False,
            "added_minutes": 0,
            "energy": round(self.energy, 1),
            "spoken_text": None,
            "trigger_user": display_name,
            "status": self.status(now=now_ts),
        }

    def on_gift(
        self,
        *,
        user_id: str | None,
        display_name: str,
        gift_name: str,
        coins: int | float,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Apply high-weight gift energy and extend the window if threshold is reached."""
        if not self.active:
            return None
        now_ts = self._now(now)
        coin_val = max(0.0, float(coins or 0))
        gain = max(12.0, coin_val * self.gift_coin_multiplier)
        return self._apply_energy(
            gain,
            source="gift",
            display_name=display_name or (user_id or "观众"),
            gift_name=gift_name or "礼物",
            now_ts=now_ts,
        )

    def on_chat(
        self,
        *,
        user_id: str | None,
        display_name: str,
        text: str,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Apply bounded chat energy with per-user cooldown to prevent single-user spam."""
        if not self.active:
            return None
        cleaned = (text or "").strip()
        if not cleaned:
            return None
        now_ts = self._now(now)
        user_key = str(user_id or f"anon:{display_name or 'viewer'}")
        last_at = self._last_chat_by_user.get(user_key)
        if last_at is not None and (now_ts - last_at) < self.chat_cooldown_seconds:
            return None
        self._last_chat_by_user[user_key] = now_ts
        if len(self._last_chat_by_user) > 1000:
            cutoff = now_ts - max(60.0, self.chat_cooldown_seconds * 4)
            self._last_chat_by_user = {
                k: ts for k, ts in self._last_chat_by_user.items() if ts >= cutoff
            }
            while len(self._last_chat_by_user) > 1000:
                self._last_chat_by_user.pop(next(iter(self._last_chat_by_user)))
        gain = min(max(self.chat_base_energy + 4.0, 10.0), self.chat_base_energy + min(4.0, len(cleaned) * 0.2))
        return self._apply_energy(
            gain,
            source="chat",
            display_name=display_name or "观众",
            now_ts=now_ts,
        )

    def tick(self, now: float | None = None) -> dict[str, Any] | None:
        """Decay energy gently and emit closing-soon / window-ended cues."""
        if not self.active or self.scheduled_end_at is None:
            return None
        now_ts = self._now(now)
        if self.last_tick_at is not None and now_ts > self.last_tick_at:
            elapsed_min = (now_ts - self.last_tick_at) / 60.0
            self.energy = max(0.0, self.energy - elapsed_min * self.decay_per_minute)
        self.last_tick_at = now_ts

        remaining = self.scheduled_end_at - now_ts
        if remaining <= 0:
            self.active = False
            self.stage = "ended"
            if not self._ended_cue_emitted:
                self._ended_cue_emitted = True
                self.last_event_line = _ENDED_LINE
                return {
                    "kind": "window_ended",
                    "stage": "ended",
                    "spoken_text": _ENDED_LINE,
                    "status": self.status(now=now_ts),
                }
            return None

        if remaining <= 300:
            self.stage = "closing_soon"
            if not self._closing_cue_emitted:
                self._closing_cue_emitted = True
                self.last_event_line = _CLOSING_SOON_LINE
                return {
                    "kind": "closing_soon",
                    "stage": "closing_soon",
                    "spoken_text": _CLOSING_SOON_LINE,
                    "status": self.status(now=now_ts),
                }
        else:
            self.stage = "overtime" if self.overtime_minutes > 0 else "working"
        return None

    def status(self, now: float | None = None) -> dict[str, Any]:
        """Return overlay-safe and workbench-ready Overtime Theater status."""
        now_ts = self._now(now)
        remaining = (
            max(0, int(self.scheduled_end_at - now_ts))
            if self.active and self.scheduled_end_at is not None
            else 0
        )
        stage = self.stage
        if self.active and self.scheduled_end_at is not None:
            if remaining <= 0:
                stage = "ended"
            elif remaining <= 300:
                stage = "closing_soon"
            elif self.overtime_minutes > 0:
                stage = "overtime"
            else:
                stage = "working"
        return {
            "active": self.active,
            "energy": round(self.energy, 1),
            "threshold": self.threshold,
            "energy_percent": round(min(100.0, (self.energy / self.threshold) * 100.0), 1),
            "base_duration_minutes": self.base_duration_minutes,
            "overtime_minutes": self.overtime_minutes,
            "max_overtime_minutes": self.max_overtime_minutes,
            "extension_step_minutes": self.extension_step_minutes,
            "extensions_count": self.extensions_count,
            "remaining_seconds": remaining,
            "started_at": self.started_at,
            "base_end_at": self.base_end_at,
            "scheduled_end_at": self.scheduled_end_at,
            "stage": stage,
            "last_event_line": self.last_event_line,
        }

    def stop(self) -> dict[str, Any]:
        """End the Overtime Theater window cleanly."""
        self.active = False
        self.stage = "stopped"
        self.last_event_line = "本场加班小剧场已收工，去盛白饭咯。"
        return self.status()
