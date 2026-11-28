"""Live rundown sequencer, interactive game-pool ballot, presets, and fine-grained show defaults."""

from __future__ import annotations

import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from ...core.models import StrictModel
from ..activity_filter import LivePoll
from ..media import ObsLayoutId

RundownSegmentKind = Literal[
    "chat",
    "draw_and_guess",
    "turtle_soup",
    "roast_radio",
    "song_party",
    "stardew_valley",
    "pvz",
]

GAME_CATALOG: list[dict[str, str]] = [
    {
        "kind": "draw_and_guess",
        "label": "你画我猜 2.0",
        "default_layout": "game",
        "description": "Aqua 矢量画板 + 渐进提示 + 弹幕抢答",
    },
    {
        "kind": "turtle_soup",
        "label": "灵魂海龟汤",
        "default_layout": "game",
        "description": "悬疑/日常/科幻海龟汤推理互动",
    },
    {
        "kind": "roast_radio",
        "label": "锐评辩论",
        "default_layout": "chat",
        "description": "公开见闻吐槽电台与正反方弹幕辩论",
    },
    {
        "kind": "song_party",
        "label": "点歌听歌会",
        "default_layout": "chat",
        "description": "弹幕点歌与歌词陪伴歌唱环节",
    },
    {
        "kind": "stardew_valley",
        "label": "星露谷物语",
        "default_layout": "game",
        "description": "实机/模拟农场种田与礼物互动",
    },
    {
        "kind": "pvz",
        "label": "植物大战僵尸",
        "default_layout": "game",
        "description": "实机/模拟塔防闯关与弹幕支援",
    },
]

GAME_CATALOG_MAP: dict[str, dict[str, str]] = {item["kind"]: item for item in GAME_CATALOG}


class DrawAndGuessFineSettings(StrictModel):
    duration_sec: int = Field(default=75, ge=15, le=300)
    use_llm: bool = True
    mode: Literal["aqua_draws", "viewer_draws"] = "aqua_draws"
    hint_l2_ratio: float = Field(default=0.35, ge=0.1, le=0.8)
    hint_l3_ratio: float = Field(default=0.70, ge=0.2, le=0.95)
    default_theme: str = Field(default="", max_length=60)


class TurtleSoupFineSettings(StrictModel):
    use_llm: bool = True
    default_genre: str = Field(default="mystery", max_length=30)
    max_questions: int = Field(default=20, ge=5, le=100)
    auto_hint_every_n_questions: int = Field(default=5, ge=0, le=30)


class RundownSegment(StrictModel):
    id: str = Field(default_factory=lambda: f"seg-{uuid4().hex[:8]}", min_length=1, max_length=60)
    title: str = Field(min_length=1, max_length=80)
    kind: RundownSegmentKind
    duration_minutes: int = Field(default=15, ge=5, le=180)
    obs_layout: ObsLayoutId = "chat"
    auto_start_show: bool = True
    graceful_finish: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("环节名称不能为空")
        return cleaned


class ChatEntryConfig(StrictModel):
    opening_mode: Literal["chat_opening", "direct_rundown", "manual"] = "chat_opening"
    opening_chat_minutes: int = Field(default=15, ge=5, le=180)
    poll_duration_seconds: int = Field(default=90, ge=15, le=600)
    min_votes: int = Field(default=1, ge=0, le=100)
    include_continue_chat: bool = True
    avoid_repeat: bool = True
    allow_chat_vote_trigger: bool = True
    vote_trigger_threshold: int = Field(default=2, ge=1, le=50)
    allow_chat_extend: bool = True
    chat_adaptive_enabled: bool = True
    chat_hot_threshold_per_2m: int = Field(default=12, ge=1, le=200)


class RundownPlan(StrictModel):
    name: str = Field(default="多游戏轮播→闲聊", min_length=1, max_length=80)
    enabled_games: list[str] = Field(
        default_factory=lambda: ["draw_and_guess", "turtle_soup", "roast_radio", "song_party"],
        max_length=10,
    )
    hold_on_last: bool = True
    auto_switch_layout: bool = True
    chat_entry: ChatEntryConfig = Field(default_factory=ChatEntryConfig)
    segments: list[RundownSegment] = Field(min_length=1, max_length=24)

    @field_validator("enabled_games")
    @classmethod
    def validate_enabled_games(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        for item in values:
            if item not in GAME_CATALOG_MAP:
                raise ValueError(f"未知游戏池选项: {item}")
            if item not in out:
                out.append(item)
        return out


def build_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": "multi_game_chat",
            "name": "多游戏轮播→闲聊",
            "description": "15分钟热聊开局 → 投票选游戏（你画我猜/海龟汤/锐评/点歌）→ 回到闲聊收尾",
            "plan": RundownPlan(
                name="多游戏轮播→闲聊",
                enabled_games=["draw_and_guess", "turtle_soup", "roast_radio", "song_party"],
                hold_on_last=True,
                auto_switch_layout=True,
                chat_entry=ChatEntryConfig(opening_mode="chat_opening", opening_chat_minutes=15),
                segments=[
                    RundownSegment(
                        id="seg-opening-chat",
                        title="开播热聊",
                        kind="chat",
                        duration_minutes=15,
                        obs_layout="chat",
                        auto_start_show=False,
                        graceful_finish=False,
                    ),
                    RundownSegment(
                        id="seg-draw-guess",
                        title="你画我猜 2.0",
                        kind="draw_and_guess",
                        duration_minutes=20,
                        obs_layout="game",
                        auto_start_show=True,
                        graceful_finish=True,
                        settings={"mode": "aqua_draws", "use_llm": True},
                    ),
                    RundownSegment(
                        id="seg-turtle-soup",
                        title="灵魂海龟汤",
                        kind="turtle_soup",
                        duration_minutes=20,
                        obs_layout="game",
                        auto_start_show=True,
                        graceful_finish=True,
                        settings={"genre": "mystery", "use_llm": True},
                    ),
                    RundownSegment(
                        id="seg-closing-chat",
                        title="下播前闲聊",
                        kind="chat",
                        duration_minutes=15,
                        obs_layout="chat",
                        auto_start_show=False,
                        graceful_finish=False,
                    ),
                ],
            ).model_dump(),
        },
        {
            "id": "device_and_shows",
            "name": "游戏实机+互动→闲聊",
            "description": "15分钟热聊开局 → 星露谷/植物大战僵尸实机与小游戏穿插 → 闲聊收尾",
            "plan": RundownPlan(
                name="游戏实机+互动→闲聊",
                enabled_games=["stardew_valley", "pvz", "draw_and_guess", "turtle_soup"],
                hold_on_last=True,
                auto_switch_layout=True,
                chat_entry=ChatEntryConfig(opening_mode="chat_opening", opening_chat_minutes=15),
                segments=[
                    RundownSegment(
                        id="seg-opening-chat",
                        title="开播闲聊",
                        kind="chat",
                        duration_minutes=15,
                        obs_layout="chat",
                        auto_start_show=False,
                        graceful_finish=False,
                    ),
                    RundownSegment(
                        id="seg-stardew",
                        title="星露谷种田",
                        kind="stardew_valley",
                        duration_minutes=30,
                        obs_layout="game",
                        auto_start_show=True,
                        graceful_finish=False,
                    ),
                    RundownSegment(
                        id="seg-pvz",
                        title="植物大战僵尸",
                        kind="pvz",
                        duration_minutes=25,
                        obs_layout="game",
                        auto_start_show=True,
                        graceful_finish=False,
                    ),
                    RundownSegment(
                        id="seg-closing-chat",
                        title="晚安闲聊",
                        kind="chat",
                        duration_minutes=15,
                        obs_layout="chat",
                        auto_start_show=False,
                        graceful_finish=False,
                    ),
                ],
            ).model_dump(),
        },
        {
            "id": "late_night_llm",
            "name": "纯LLM轻综艺夜场",
            "description": "15分钟夜间闲聊 → 锐评辩论 / 海龟汤 / 点歌听歌会循环",
            "plan": RundownPlan(
                name="纯LLM轻综艺夜场",
                enabled_games=["roast_radio", "turtle_soup", "song_party", "draw_and_guess"],
                hold_on_last=True,
                auto_switch_layout=True,
                chat_entry=ChatEntryConfig(opening_mode="chat_opening", opening_chat_minutes=15),
                segments=[
                    RundownSegment(
                        id="seg-opening-chat",
                        title="深夜闲聊开场",
                        kind="chat",
                        duration_minutes=15,
                        obs_layout="chat",
                        auto_start_show=False,
                        graceful_finish=False,
                    ),
                    RundownSegment(
                        id="seg-roast-radio",
                        title="深夜锐评电台",
                        kind="roast_radio",
                        duration_minutes=20,
                        obs_layout="chat",
                        auto_start_show=True,
                        graceful_finish=False,
                    ),
                    RundownSegment(
                        id="seg-turtle-soup",
                        title="海龟汤推理夜",
                        kind="turtle_soup",
                        duration_minutes=20,
                        obs_layout="game",
                        auto_start_show=True,
                        graceful_finish=True,
                    ),
                    RundownSegment(
                        id="seg-song-party",
                        title="夜间点歌听歌会",
                        kind="song_party",
                        duration_minutes=20,
                        obs_layout="chat",
                        auto_start_show=False,
                        graceful_finish=False,
                    ),
                ],
            ).model_dump(),
        },
    ]


def default_rundown_plan() -> RundownPlan:
    return RundownPlan.model_validate(build_presets()[0]["plan"])


class RundownManager:
    """Manages active rundown segments, OBS layout switching, game-pool votes, and graceful finish."""

    def __init__(self, session) -> None:
        self.session = session
        self.active: bool = False
        self.paused: bool = False
        self.status_label: str = "idle"
        self.current_index: int = -1
        self.active_segment: dict[str, Any] | None = None
        self.segment_started_at: float | None = None
        self.segment_end_at: float | None = None
        self.paused_remaining: float | None = None
        self.waiting_graceful_finish: bool = False
        self.hot_chat_prompt: bool = False
        self.last_transition_line: str = ""
        self._last_played_game: str | None = None
        self._opening_voted: bool = False
        self._pending_vote: dict[str, Any] | None = None
        self._vote_trigger_voters: set[str] = set()
        self._last_chat_extend_at: float = 0.0

    def plan(self) -> RundownPlan:
        raw = self.session.store.get("live_rundown_plan")
        if raw:
            try:
                return RundownPlan.model_validate(raw)
            except Exception:
                pass
        return default_rundown_plan()

    def save_plan(self, payload: dict[str, Any] | RundownPlan) -> RundownPlan:
        if isinstance(payload, dict) and "preset_id" in payload and len(payload) == 1:
            preset_id = payload["preset_id"]
            preset = next((p for p in build_presets() if p["id"] == preset_id), None)
            if preset is None:
                raise ValueError("未知节目单预设")
            model = RundownPlan.model_validate(preset["plan"])
        elif isinstance(payload, RundownPlan):
            model = payload
        else:
            model = RundownPlan.model_validate(payload)
        self.session.store.set("live_rundown_plan", model.model_dump())
        return model

    def _apply_layout(self, layout: ObsLayoutId, plan: RundownPlan) -> bool:
        if not plan.auto_switch_layout:
            return False
        ls = self.session.media.layout_state()
        if ls["layout_locked"] or ls["active_layout"] == layout:
            return False
        self.session.media.set_layout(layout=layout)
        return True

    def _cleanup_previous_show(self, prev_kind: str | None, next_kind: str | None, *, force: bool) -> None:
        if not prev_kind or prev_kind == next_kind:
            return
        if prev_kind == "draw_and_guess":
            if self.session.draw_and_guess.is_active() and force:
                self.session.draw_and_guess.stop_round(reason="节目单切换环节")
            if self.session.draw_and_guess.state != "idle":
                self.session.draw_and_guess.state = "idle"
        elif prev_kind == "turtle_soup":
            if self.session.turtle_soup.is_active() and force:
                self.session.turtle_soup.reveal_and_stop()
            if self.session.turtle_soup.state != "idle":
                self.session.turtle_soup.state = "idle"
        elif prev_kind == "roast_radio" and self.session.roast_radio.is_active():
            self.session.roast_radio.stop()

    async def _launch_segment_show(self, segment: RundownSegment) -> None:
        if not segment.auto_start_show:
            return
        settings = segment.settings or {}
        fine = self.session.fine_settings()
        if segment.kind == "draw_and_guess":
            if not self.session.draw_and_guess.is_active():
                dg_fine = fine["draw_and_guess"]
                use_llm = bool(settings.get("use_llm", dg_fine["use_llm"]))
                duration_sec = int(settings.get("duration_sec", dg_fine["duration_sec"]))
                mode = str(settings.get("mode", dg_fine["mode"]))
                theme = settings.get("theme") or dg_fine.get("default_theme") or None
                if use_llm:
                    await self.session.draw_and_guess.start_round_llm(
                        self.session.runtime.llm,
                        trigger_user="节目单",
                        duration_sec=duration_sec,
                        theme=theme,
                        mode=mode,
                    )
                else:
                    self.session.draw_and_guess.start_round(
                        trigger_user="节目单",
                        duration_sec=duration_sec,
                        theme=theme,
                        mode=mode,
                    )
        elif segment.kind == "turtle_soup":
            if not self.session.turtle_soup.is_active():
                ts_fine = fine["turtle_soup"]
                use_llm = bool(settings.get("use_llm", ts_fine["use_llm"]))
                genre = settings.get("genre") or ts_fine.get("default_genre") or None
                if use_llm:
                    await self.session.turtle_soup.start_round_llm(
                        self.session.runtime.llm,
                        trigger_user="节目单",
                        genre=genre,
                    )
                else:
                    self.session.turtle_soup.start_round(trigger_user="节目单", genre=genre)
        elif segment.kind == "roast_radio":
            if not self.session.roast_radio.is_active():
                if settings.get("mode") == "debate" and settings.get("topic"):
                    self.session.roast_radio.start_debate(
                        topic=str(settings["topic"]),
                        side_a=str(settings.get("side_a") or "正方"),
                        side_b=str(settings.get("side_b") or "反方"),
                        trigger_user="节目单",
                    )
                else:
                    await self.session.roast_radio.generate_roast_segment(
                        self.session.runtime.llm,
                        self.session.store,
                        topic=settings.get("topic"),
                    )
        elif segment.kind in {"stardew_valley", "pvz"}:
            if self.session.game_livestream.active_game_id != segment.kind:
                self.session.game_livestream.switch_game(segment.kind, reason="节目单自动切换")

    async def _enter_segment(
        self,
        segment: RundownSegment,
        *,
        index: int,
        plan: RundownPlan,
        now: float,
        prev_kind: str | None = None,
        force: bool = True,
        transition_line: str = "",
    ) -> dict[str, Any]:
        self._cleanup_previous_show(prev_kind, segment.kind, force=force)
        seg_dict = segment.model_dump()
        self.active = True
        self.paused = False
        self.status_label = "running"
        self.current_index = index
        self.active_segment = seg_dict
        self.segment_started_at = now
        self.segment_end_at = now + segment.duration_minutes * 60.0
        self.paused_remaining = None
        self.waiting_graceful_finish = False
        self.hot_chat_prompt = False
        self.last_transition_line = transition_line or f"已进入环节「{segment.title}」"
        scene_changed = self._apply_layout(segment.obs_layout, plan)
        if segment.kind in GAME_CATALOG_MAP:
            self._last_played_game = segment.kind
        try:
            await self._launch_segment_show(segment)
        except Exception:
            pass
        return {
            "transition": "segment",
            "segment": seg_dict,
            "scene_changed": scene_changed,
        }

    async def start(self, *, now: float | None = None, mode: str | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        opening_mode = mode or plan.chat_entry.opening_mode
        self._opening_voted = False
        self._pending_vote = None
        self._vote_trigger_voters.clear()
        prev_kind = (self.active_segment or {}).get("kind")

        if opening_mode == "chat_opening":
            first = plan.segments[0]
            if first.kind == "chat":
                seg = first.model_copy(
                    update={"duration_minutes": plan.chat_entry.opening_chat_minutes, "obs_layout": "chat"}
                )
            else:
                seg = RundownSegment(
                    id="seg-opening-chat",
                    title="开播热聊",
                    kind="chat",
                    duration_minutes=plan.chat_entry.opening_chat_minutes,
                    obs_layout="chat",
                    auto_start_show=False,
                    graceful_finish=False,
                )
            return await self._enter_segment(
                seg,
                index=0,
                plan=plan,
                now=now_ts,
                prev_kind=prev_kind,
                force=True,
                transition_line=f"开播进入 {seg.duration_minutes} 分钟闲聊开场（chat 布局）",
            )

        first = plan.segments[0]
        return await self._enter_segment(
            first,
            index=0,
            plan=plan,
            now=now_ts,
            prev_kind=prev_kind,
            force=True,
            transition_line=f"按节目单进入「{first.title}」",
        )

    def pause(self, *, now: float | None = None) -> dict[str, Any]:
        if not self.active or self.paused:
            return self.state(now=now)
        now_ts = time.time() if now is None else float(now)
        self.paused = True
        self.status_label = "paused"
        self.paused_remaining = max(0.0, (self.segment_end_at or now_ts) - now_ts)
        return self.state(now=now_ts)

    def resume(self, *, now: float | None = None) -> dict[str, Any]:
        if not self.active or not self.paused:
            return self.state(now=now)
        now_ts = time.time() if now is None else float(now)
        rem = self.paused_remaining if self.paused_remaining is not None else 0.0
        self.paused = False
        self.status_label = "voting" if self._pending_vote else "running"
        self.segment_end_at = now_ts + rem
        self.paused_remaining = None
        return self.state(now=now_ts)

    def stop(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        if self._pending_vote and self.session.activity_filter.active_poll:
            self.session.finish_poll()
        self.active = False
        self.paused = False
        self.status_label = "stopped"
        self.current_index = -1
        self.active_segment = None
        self.segment_started_at = None
        self.segment_end_at = None
        self.paused_remaining = None
        self.waiting_graceful_finish = False
        self.hot_chat_prompt = False
        self._pending_vote = None
        self._vote_trigger_voters.clear()
        self.last_transition_line = "节目单已停止"
        return self.state(now=now_ts)

    def _next_plan_index(self, plan: RundownPlan) -> int:
        if not plan.segments:
            return 0
        nxt = self.current_index + 1
        if nxt < len(plan.segments):
            return nxt
        return len(plan.segments) - 1 if plan.hold_on_last else 0

    def _has_active_graceful_show(self, segment: dict[str, Any] | None) -> bool:
        if not segment or not segment.get("graceful_finish"):
            return False
        kind = segment.get("kind")
        if kind == "draw_and_guess":
            return self.session.draw_and_guess.is_active()
        if kind == "turtle_soup":
            return self.session.turtle_soup.is_active()
        return False

    async def next(self, *, force: bool = True, now: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        if not self.active:
            return await self.start(now=now_ts)
        if not force and self._has_active_graceful_show(self.active_segment):
            self.waiting_graceful_finish = True
            self.status_label = "waiting_graceful"
            return {"transition": "waiting_graceful", "scene_changed": False}
        if self._pending_vote and self.session.activity_filter.active_poll:
            self.session.finish_poll()
        self._pending_vote = None
        nxt = self.current_index + 1
        prev_kind = (self.active_segment or {}).get("kind")
        if nxt >= len(plan.segments):
            if plan.hold_on_last:
                last_seg = plan.segments[-1]
                self.waiting_graceful_finish = False
                self.status_label = "running"
                self.segment_started_at = now_ts
                self.segment_end_at = now_ts + last_seg.duration_minutes * 60.0
                self.last_transition_line = f"已在末段「{last_seg.title}」保持运行"
                scene_changed = self._apply_layout(last_seg.obs_layout, plan)
                return {"transition": "hold_last", "segment": self.active_segment, "scene_changed": scene_changed}
            self.active = False
            self.status_label = "completed"
            self.waiting_graceful_finish = False
            self._cleanup_previous_show(prev_kind, None, force=True)
            self.last_transition_line = "本场节目单已全部完成"
            return {"transition": "completed", "scene_changed": False}

        target = plan.segments[nxt]
        return await self._enter_segment(
            target,
            index=nxt,
            plan=plan,
            now=now_ts,
            prev_kind=prev_kind,
            force=force,
        )

    async def jump(
        self,
        *,
        index: int | None = None,
        segment_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        target_idx: int | None = None
        if segment_id is not None:
            for idx, seg in enumerate(plan.segments):
                if seg.id == segment_id:
                    target_idx = idx
                    break
        elif index is not None:
            if 0 <= index < len(plan.segments):
                target_idx = index
        if target_idx is None:
            raise ValueError("找不到指定的节目单环节")
        if self._pending_vote and self.session.activity_filter.active_poll:
            self.session.finish_poll()
        self._pending_vote = None
        prev_kind = (self.active_segment or {}).get("kind")
        target = plan.segments[target_idx]
        return await self._enter_segment(
            target,
            index=target_idx,
            plan=plan,
            now=now_ts,
            prev_kind=prev_kind,
            force=True,
            transition_line=f"已跳转至环节「{target.title}」",
        )

    async def extend_chat(
        self,
        *,
        minutes: int = 15,
        now: float | None = None,
        transition_line: str | None = None,
    ) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        mins = max(5, min(180, int(minutes)))
        self._last_chat_extend_at = now_ts
        if self.active and self.active_segment and self.active_segment.get("kind") == "chat":
            rem = max(0.0, (self.segment_end_at or now_ts) - now_ts)
            self.segment_end_at = now_ts + rem + mins * 60.0
            if self.paused:
                self.paused_remaining = (self.paused_remaining or 0.0) + mins * 60.0
            self.status_label = "paused" if self.paused else "running"
            self.last_transition_line = transition_line or f"继续闲聊延长 {mins} 分钟"
            scene_changed = self._apply_layout("chat", plan)
            return {"transition": "extend_chat", "segment": self.active_segment, "scene_changed": scene_changed}

        prev_kind = (self.active_segment or {}).get("kind")
        seg = RundownSegment(
            id=f"seg-chat-{uuid4().hex[:6]}",
            title="继续闲聊",
            kind="chat",
            duration_minutes=mins,
            obs_layout="chat",
            auto_start_show=False,
            graceful_finish=False,
        )
        return await self._enter_segment(
            seg,
            index=max(0, self.current_index),
            plan=plan,
            now=now_ts,
            prev_kind=prev_kind,
            force=True,
            transition_line=transition_line or f"切换回闲聊，延长 {mins} 分钟",
        )

    async def open_game_vote(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        enabled = [g for g in plan.enabled_games if g in GAME_CATALOG_MAP]
        if not enabled:
            enabled = ["draw_and_guess", "turtle_soup", "roast_radio", "song_party"]
        if plan.chat_entry.avoid_repeat and self._last_played_game in enabled and len(enabled) > 1:
            pool = [g for g in enabled if g != self._last_played_game]
        else:
            pool = list(enabled)

        options: list[str] = []
        option_to_kind: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for kind in pool[:4]:
            item = GAME_CATALOG_MAP[kind]
            label = item["label"]
            options.append(label)
            option_to_kind[label] = kind
            aliases[kind] = label

        if plan.chat_entry.include_continue_chat and len(options) < 9:
            continue_label = "继续闲聊"
            options.append(continue_label)
            option_to_kind[continue_label] = "chat"
            aliases["继续聊"] = continue_label

        if len(options) < 2:
            # LivePoll requires >=2 options
            options.append("继续闲聊")
            option_to_kind["继续闲聊"] = "chat"

        poll_req = LivePoll(
            platform="*",
            room_id="*",
            topic="下一环节玩什么？",
            options=options,
            duration_seconds=float(plan.chat_entry.poll_duration_seconds),
            aliases=aliases,
            fallback_label="无人投票将按默认节目单推进",
        )
        activity = self.session.start_poll(poll_req)
        self._opening_voted = True
        self._vote_trigger_voters.clear()
        self._pending_vote = {"poll_id": activity["id"], "option_to_kind": option_to_kind}
        if not self.active:
            self.active = True
            self.current_index = 0
            self.active_segment = plan.segments[0].model_dump()
            self.segment_started_at = now_ts
            self.segment_end_at = now_ts + plan.chat_entry.poll_duration_seconds
        self.status_label = "voting"
        self.last_transition_line = "已发起游戏池投票！发送数字或游戏名即可参与投票"
        return {"poll": activity, "option_to_kind": option_to_kind}

    def _segment_for_voted_kind(self, kind: str, plan: RundownPlan) -> tuple[RundownSegment, int]:
        for idx, seg in enumerate(plan.segments):
            if seg.kind == kind and idx > self.current_index:
                return seg, idx
        for idx, seg in enumerate(plan.segments):
            if seg.kind == kind:
                return seg, idx
        cat = GAME_CATALOG_MAP[kind]
        seg = RundownSegment(
            id=f"seg-vote-{kind}",
            title=cat["label"],
            kind=kind,  # type: ignore[arg-type]
            duration_minutes=20,
            obs_layout=cat["default_layout"],  # type: ignore[arg-type]
            auto_start_show=kind in {"draw_and_guess", "turtle_soup", "roast_radio", "stardew_valley", "pvz"},
            graceful_finish=kind in {"draw_and_guess", "turtle_soup"},
        )
        return seg, max(0, self.current_index)

    async def settle_vote(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        pending = self._pending_vote
        self._pending_vote = None
        result = self.session.finish_poll()
        if result is None and pending:
            for item in reversed(self.session.recent_activities):
                if item.get("id") == pending.get("poll_id") and item.get("phase") == "ended":
                    result = item
                    break

        option_to_kind = (pending or {}).get("option_to_kind", {})
        total_votes = int((result or {}).get("total_votes") or 0)
        prev_kind = (self.active_segment or {}).get("kind")

        if result is None or total_votes < plan.chat_entry.min_votes:
            next_idx = self._next_plan_index(plan)
            target_seg = plan.segments[next_idx]
            entered = await self._enter_segment(
                target_seg,
                index=next_idx,
                plan=plan,
                now=now_ts,
                prev_kind=prev_kind,
                force=True,
                transition_line=f"投票人数不足，按默认节目单进入「{target_seg.title}」",
            )
            return entered | {"fallback": True, "vote_result": result}

        winner_opt = result.get("winner")
        if not winner_opt:
            winners = result.get("winners") or []
            winner_opt = winners[0] if winners else None
        chosen_kind = option_to_kind.get(winner_opt) if winner_opt else None
        if not chosen_kind:
            next_idx = self._next_plan_index(plan)
            target_seg = plan.segments[next_idx]
            entered = await self._enter_segment(
                target_seg,
                index=next_idx,
                plan=plan,
                now=now_ts,
                prev_kind=prev_kind,
                force=True,
                transition_line=f"按默认节目单进入「{target_seg.title}」",
            )
            return entered | {"fallback": True, "vote_result": result}

        if chosen_kind == "chat":
            ext = await self.extend_chat(
                minutes=15,
                now=now_ts,
                transition_line="观众投票选择继续闲聊，再聊 15 分钟！",
            )
            return ext | {"fallback": False, "chosen_kind": "chat", "vote_result": result}

        target_seg, target_idx = self._segment_for_voted_kind(chosen_kind, plan)
        entered = await self._enter_segment(
            target_seg,
            index=target_idx,
            plan=plan,
            now=now_ts,
            prev_kind=prev_kind,
            force=True,
            transition_line=f"投票胜出：「{target_seg.title}」，进入 {target_seg.obs_layout} 布局！",
        )
        return entered | {"fallback": False, "chosen_kind": chosen_kind, "vote_result": result}

    def next_wake_delay(self, *, now: float | None = None) -> float | None:
        if not self.active or self.paused:
            return None
        now_ts = time.time() if now is None else float(now)
        if self._pending_vote and self.session.activity_filter.is_poll_active():
            poll = self.session.activity_filter.public_poll()
            if poll is not None:
                return max(0.05, min(1.0, float(poll["remaining_seconds"])))
        if self.segment_end_at is not None:
            rem = max(0.0, self.segment_end_at - now_ts)
            return max(0.05, min(1.0, rem))
        return 1.0

    async def tick(self, *, now: float | None = None) -> dict[str, Any] | None:
        if not self.active or self.paused:
            return None
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()

        # Settle expired game-pool ballot
        if self._pending_vote:
            self.session.expire_poll()
            if not self.session.activity_filter.is_poll_active():
                return await self.settle_vote(now=now_ts)
            return None

        # Hot-chat indicator
        if plan.chat_entry.chat_adaptive_enabled and (self.active_segment or {}).get("kind") == "chat":
            cutoff = now_ts - 120.0
            recent_count = sum(1 for item in self.session.recent_barrage if float(item.get("at", 0.0)) >= cutoff)
            self.hot_chat_prompt = recent_count >= plan.chat_entry.chat_hot_threshold_per_2m
        else:
            self.hot_chat_prompt = False

        if self.segment_end_at is None or now_ts < self.segment_end_at:
            return None

        # Segment timer expired
        if self._has_active_graceful_show(self.active_segment):
            self.waiting_graceful_finish = True
            self.status_label = "waiting_graceful"
            return None

        self.waiting_graceful_finish = False
        if (
            self.current_index == 0
            and (self.active_segment or {}).get("kind") == "chat"
            and plan.chat_entry.opening_mode == "chat_opening"
            and not self._opening_voted
            and plan.enabled_games
        ):
            try:
                vote_info = await self.open_game_vote(now=now_ts)
                return {"transition": "vote_opened", "scene_changed": False, **vote_info}
            except Exception:
                pass

        return await self.next(force=False, now=now_ts)

    def state(self, *, now: float | None = None) -> dict[str, Any]:
        now_ts = time.time() if now is None else float(now)
        plan = self.plan()
        if not self.active:
            remaining = 0.0
        elif self.paused:
            remaining = max(0.0, float(self.paused_remaining or 0.0))
        elif self.segment_end_at is not None:
            remaining = max(0.0, float(self.segment_end_at - now_ts))
        else:
            remaining = 0.0

        next_seg = None
        if self.active and 0 <= self.current_index + 1 < len(plan.segments):
            next_seg = plan.segments[self.current_index + 1].model_dump()
        elif not self.active and plan.segments:
            next_seg = plan.segments[0].model_dump()

        return {
            "active": self.active,
            "paused": self.paused,
            "status": self.status_label,
            "current_index": self.current_index,
            "current_segment": self.active_segment,
            "next_segment": next_seg,
            "remaining_seconds": round(remaining, 1),
            "waiting_graceful_finish": self.waiting_graceful_finish,
            "hot_chat_prompt": self.hot_chat_prompt,
            "last_transition_line": self.last_transition_line,
        }

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        return {
            "plan": self.plan().model_dump(),
            "state": self.state(now=now),
            "presets": build_presets(),
            "game_catalog": list(GAME_CATALOG),
        }
