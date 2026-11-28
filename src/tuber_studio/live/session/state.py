"""Live session state container, control lifecycle and timezone/options access."""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from uuid import uuid4

from ...core.store import character_card
from ...persona.characters.performance import performance
from ..activity_filter import LiveActivityFilter
from ..games.draw_and_guess import DrawAndGuessService
from ..games.game_livestream import GameLivestreamService
from ..games.interactive_rules import InteractiveRulesService
from ..games.roast_radio import RoastRadioService
from ..games.turtle_soup import TurtleSoupService
from ..media import LiveMedia
from ..music.online_songs import OnlineSongs
from ..options import LiveLookup, LiveOptions
from .cold_director import ColdRoomDirector
from .overtime_theater import OvertimeTheater
from .rundown import DrawAndGuessFineSettings, RundownManager, TurtleSoupFineSettings
from .schedules import LiveSchedules

TERMINAL = {"completed", "failed", "cancelled", "expired"}
HANDOFF_PLATFORM = "operator_handoff"
IDLE_AFTER = 90
IDLE_COOLDOWN = 180
IDLE_WINDOW = 600
LIVE_OUTCOMES = (
    "draw_guess_won",
    "draw_guess_near_miss",
    "draw_guess_start",
    "game_switch",
    "game_action",
    "turtle_soup_start",
    "turtle_soup_qa",
    "turtle_soup_solved",
    "roast_radio_debate",
    "roast_radio_vote",
    "roast_radio_roast",
    "overtime_extended",
    "cold_wakeup",
    "rundown_vote_opened",
    "rundown_vote_progress",
    "rundown_chat_extended",
)


class LiveSessionState:
    def __init__(
        self,
        runtime,
        *,
        singing_provider=None,
        live_scene=None,
        timezone_provider: Callable[[], str] | None = None,
    ):
        self.runtime = runtime
        self._timezone_provider = timezone_provider
        self.settings, self.store = runtime.settings, runtime.store
        self.interactive_rules = InteractiveRulesService(self.store)
        self.draw_and_guess = DrawAndGuessService(daily_memory=runtime.daily_memory)
        self.turtle_soup = TurtleSoupService(daily_memory=runtime.daily_memory)
        self.roast_radio = RoastRadioService()
        self.game_livestream = GameLivestreamService(interactive_rules=self.interactive_rules, store=self.store)
        self.cold_director = ColdRoomDirector()
        self.overtime_theater = OvertimeTheater()
        self.live_schedules = LiveSchedules(self)
        self.rundown = RundownManager(self)
        self._draw_guess_fine = DrawAndGuessFineSettings().model_dump()
        self._turtle_soup_fine = TurtleSoupFineSettings().model_dump()
        self._load_fine_settings()
        self.live_scene = live_scene
        self.state = "stopped"
        self.muted = False
        self.session_id = None
        self.queue = []
        self.current_id = None
        self.task = None
        self.worker = None
        self.wake = asyncio.Event()
        self.overlay = None
        self.overlay_id = None
        self.playback_done = asyncio.Event()
        self.last_played = None
        self.timings = {"llm_ms": None, "tts_ms": None, "first_audio_ms": None}
        self.accept_times = {}
        self.submissions = {}
        self.last_gift = False
        self.media = LiveMedia(self.settings, self.store, singing_provider=singing_provider)
        self.playback = None
        self.spoken_parts = []
        self.media_changed = asyncio.Event()
        self.online_songs = OnlineSongs(runtime)
        self.activity_filter = LiveActivityFilter()
        self.poll_timer = None
        self.recent_barrage = deque(maxlen=20)
        self.recent_activities = deque(maxlen=8)
        self.lookup = LiveLookup()
        self.last_activity = time.monotonic()
        self.idle_attempts = deque(maxlen=2)
        self.idle_prompt_index = 0

    @property
    def busy(self):
        return bool(self.current_id or self.queue)

    def overlay_character(self, character):
        show = performance(self.store)
        return {
            "character": character_card(character), "performance": show,
            "assets": self.settings.assets(character) if show["pets_enabled"] else [],
        }

    def new_session(self):
        self.online_songs.choices.clear()
        self.reset_poll()
        self.recent_barrage.clear()
        self.recent_activities.clear()
        self.accept_times.clear()
        self.last_played = None
        self.timings = dict.fromkeys(self.timings)
        self.idle_attempts.clear()
        self.idle_prompt_index = 0
        self.cold_director.reset()
        self.runtime.relationships.clear_live()
        self.last_activity = time.monotonic()
        self.session_id = str(uuid4())
        character = self.runtime.character
        self.store.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?)",
            (self.session_id, character["id"], character["version"], time.time(), ""),
        )

    async def control(self, action):
        if action in {"media-pause", "media-resume"}:
            if not self.playback or not self.playback.get("song"):
                raise ValueError("当前没有可暂停的歌曲")
            if self.store.job(self.current_id)["status"] != "playing":
                raise ValueError("请等待歌曲实际开始播放")
            paused = action == "media-pause"
            await self.send_overlay({"type": "media-control", "job_id": self.current_id, "paused": paused})
            return self.runtime.status()
        if action in {"start", "resume"} and not self.settings.data.get("features", {}).get(
            "live_enabled", False
        ):
            raise ValueError("直播功能已后置；当前使用非直播工作台")
        if action == "resume" and self.state == "stopped":
            raise ValueError("请先启动Mock会话")
        if action == "start":
            if self.state == "stopped":
                self.new_session()
            self.state = "live"
            self.runtime.error = None
            self.cold_director.reset()
        elif action == "pause":
            if self.state != "stopped":
                self.state = "paused"
                if self.rundown.active and not self.rundown.paused:
                    self.rundown.pause()
                if self.idle_unplayed():
                    await self.cancel_current("已暂停冷场任务")
        elif action == "resume":
            self.state = "live"
            self.runtime.error = None
            if self.rundown.active and self.rundown.paused:
                self.rundown.resume()
        elif action == "stop":
            self.state = "stopped"
            self.online_songs.choices.clear()
            self.reset_poll()
            self.recent_barrage.clear()
            self.recent_activities.clear()
            self.runtime.relationships.clear_live()
            if self.overtime_theater.active:
                self.overtime_theater.stop()
            if self.rundown.active:
                self.rundown.stop()
            self.cold_director.reset()
            self.live_schedules.on_manual_stop()
            self.clear_queue()
            await self.cancel_current("停止会话")
        elif action == "mute":
            self.muted = True
            await self.cancel_current("紧急静音")
        elif action == "unmute":
            self.muted = False
        elif action == "skip":
            await self.cancel_current("跳过当前任务")
        elif action == "clear":
            self.clear_queue()
        else:
            raise ValueError("未知控制命令")
        self.last_activity = time.monotonic()
        self.wake.set()
        self.online_songs.sync()
        return self.runtime.status()

    async def activate(self, character_id, version=None):
        self.settings.voice(self.store.managed_character(character_id, version)["voice_profile"])
        previous_state = self.state
        if previous_state == "live":
            self.state = "paused"
        try:
            self.clear_queue("角色切换")
            await self.cancel_current("角色切换")
            self.clear_queue("角色切换期间入队")
            result = self.store.activate(character_id, version)
        finally:
            self.state = previous_state
        if self.state != "stopped":
            self.new_session()
        await self.send_overlay(
            {"type": "character", **self.overlay_character(result)}
        )
        self.wake.set()
        return result

    def options(self):
        return LiveOptions.model_validate(self.store.get("live_options", {}))

    def set_options(self, options):
        if options.weather and self.settings.data.get("commercial_mode"):
            raise ValueError("当前免费天气来源用于非商业直播")
        self.store.set("live_options", options.model_dump())
        self.wake.set()
        return options

    def timezone(self):
        return (
            self.options().timezone
            or (self._timezone_provider() if self._timezone_provider else None)
            or "Asia/Taipei"
        )

    def _load_fine_settings(self) -> None:
        raw = self.store.get("live_fine_settings")
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("cold_director"), dict):
            try:
                self.cold_director.configure(raw["cold_director"])
            except ValueError:
                pass
        if isinstance(raw.get("overtime"), dict):
            try:
                self.overtime_theater.configure(raw["overtime"])
            except ValueError:
                pass
        if isinstance(raw.get("draw_and_guess"), dict):
            try:
                self._draw_guess_fine = DrawAndGuessFineSettings.model_validate(
                    self._draw_guess_fine | raw["draw_and_guess"]
                ).model_dump()
            except Exception:
                pass
        if isinstance(raw.get("turtle_soup"), dict):
            try:
                self._turtle_soup_fine = TurtleSoupFineSettings.model_validate(
                    self._turtle_soup_fine | raw["turtle_soup"]
                ).model_dump()
            except Exception:
                pass

    def fine_settings(self) -> dict:
        return {
            "cold_director": self.cold_director.config(),
            "overtime": self.overtime_theater.config(),
            "draw_and_guess": dict(self._draw_guess_fine),
            "turtle_soup": dict(self._turtle_soup_fine),
        }

    def save_fine_settings(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("fine-settings 载荷须为对象")
        if "cold_director" in patch and patch["cold_director"] is not None:
            self.cold_director.configure(patch["cold_director"])
        if "overtime" in patch and patch["overtime"] is not None:
            self.overtime_theater.configure(patch["overtime"])
        if "draw_and_guess" in patch and patch["draw_and_guess"] is not None:
            if not isinstance(patch["draw_and_guess"], dict):
                raise ValueError("draw_and_guess 配置必须为字典")
            self._draw_guess_fine = DrawAndGuessFineSettings.model_validate(
                self._draw_guess_fine | patch["draw_and_guess"]
            ).model_dump()
        if "turtle_soup" in patch and patch["turtle_soup"] is not None:
            if not isinstance(patch["turtle_soup"], dict):
                raise ValueError("turtle_soup 配置必须为字典")
            self._turtle_soup_fine = TurtleSoupFineSettings.model_validate(
                self._turtle_soup_fine | patch["turtle_soup"]
            ).model_dump()
        result = self.fine_settings()
        self.store.set("live_fine_settings", result)
        return result
