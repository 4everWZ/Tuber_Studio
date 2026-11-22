"""Public facade and safe value types exported by L3 live."""

from .activity_filter import LiveActivityFilter, LivePoll
from .avatar import SDK, LiveAvatar, avatar_package, checked_file, load_object
from .games.draw_and_guess import WORD_BANK, DrawAndGuessService, normalize_guess
from .games.game_livestream import (
    GameActionRecord,
    GameLivestreamService,
    PvZAdapter,
    StardewValleyAdapter,
)
from .games.interactive_rules import (
    DrawAndGuessRule,
    GameConfig,
    GameLivestreamRule,
    GiftActionRule,
    InteractiveRules,
    InteractiveRulesService,
    SongRequestRule,
    default_rules,
)
from .games.stardew_device import DeviceCommand, StardewDevice, register_stardew_device
from .media import (
    MAX_UPLOAD,
    MEDIA_TYPES,
    LiveMedia,
    MusicSettings,
    Scene,
    default_scene,
    inspect_audio,
    inspect_wav,
)
from .music.online_songs import OnlineSongs, SongRequestSettings, cache_bytes, parse_lrc
from .music.singing import SingingResult
from .music.song_component import SongComponent
from .music.song_download import public_address
from .options import LiveLookup, LiveOptions, WeatherLocation
from .scene import LiveScene
from .session import HANDOFF_PLATFORM, IDLE_AFTER, LiveSession, clean_barrage
from .studio import room_feed

__all__ = [
    "DeviceCommand",
    "DrawAndGuessRule",
    "DrawAndGuessService",
    "GameActionRecord",
    "GameConfig",
    "GameLivestreamRule",
    "GameLivestreamService",
    "GiftActionRule",
    "HANDOFF_PLATFORM",
    "IDLE_AFTER",
    "InteractiveRules",
    "InteractiveRulesService",
    "LiveActivityFilter",
    "LiveAvatar",
    "LiveFacade",
    "LiveLookup",
    "LiveMedia",
    "LiveOptions",
    "LivePoll",
    "LiveScene",
    "LiveSession",
    "MAX_UPLOAD",
    "MEDIA_TYPES",
    "MusicSettings",
    "OnlineSongs",
    "PvZAdapter",
    "SDK",
    "Scene",
    "SingingResult",
    "SongComponent",
    "SongRequestRule",
    "SongRequestSettings",
    "StardewDevice",
    "StardewValleyAdapter",
    "WORD_BANK",
    "WeatherLocation",
    "avatar_package",
    "cache_bytes",
    "checked_file",
    "clean_barrage",
    "create_live_facade",
    "default_rules",
    "default_scene",
    "inspect_audio",
    "inspect_wav",
    "live_facade",
    "load_object",
    "normalize_guess",
    "parse_lrc",
    "public_address",
    "register_stardew_device",
    "room_feed",
]


class LiveFacade:
    """Narrow L3 facade mediating access to live session, media, scene and games."""

    def __init__(self, session: LiveSession, *, stardew_device: StardewDevice | None = None):
        self._session = session
        self._stardew_device = stardew_device

    @property
    def session(self) -> LiveSession:
        return self._session

    @property
    def session_id(self):
        return self._session.session_id

    @property
    def stardew_device(self) -> StardewDevice | None:
        return self._stardew_device

    def attach_stardew_device(self, device: StardewDevice | None) -> StardewDevice | None:
        self._stardew_device = device
        return device

    # Lifecycle & Status
    async def open(self):
        await self._session.start_worker()

    async def close(self):
        await self._session.stop_worker()

    def is_busy(self) -> bool:
        return bool(self._session.busy)

    def state(self) -> str:
        return self._session.state

    def status(self) -> dict:
        session = self._session
        return {
            "state": session.state,
            "busy": getattr(session, "busy", False),
            "muted": getattr(session, "muted", False),
            "session_id": getattr(session, "session_id", None),
            "current_id": getattr(session, "current_id", None),
            "queue": list(getattr(session, "queue", ())),
            "playback": getattr(session, "playback", None),
            "timings": dict(getattr(session, "timings", {})),
            "recent_activities": list(getattr(session, "recent_activities", ())),
        }

    async def control(self, action: str):
        return await self._session.control(action)

    def activate_character(self, character_id, version=None):
        return self._session.activate(character_id, version=version)

    async def submit_event(self, event, *, online_snapshot=None, verified_account_id=None):
        return await self._session.submit(
            event,
            online_snapshot=online_snapshot,
            verified_account_id=verified_account_id,
        )

    def notify_store_cleaned(self):
        self._session.media_changed.set()

    async def attach_overlay(self, websocket):
        return await self._session.attach_overlay(websocket)

    async def acknowledge_overlay(self, owner, data):
        return await self._session.acknowledge(owner, data)

    async def detach_overlay(self, owner):
        return await self._session.detach_overlay(owner)

    # Settings & Scene/Music Controls
    def live_options(self) -> dict:
        return self._session.options().model_dump()

    def update_live_options(self, patch: dict) -> dict:
        current = self._session.options().model_dump()
        updated = LiveOptions.model_validate(current | patch)
        self._session.set_options(updated)
        return updated.model_dump()

    def scene_config(self) -> dict:
        return self._session.media.scene()

    def save_scene_config(self, patch: dict) -> dict:
        current = self._session.media.scene()
        return self._session.media.save_scene(current | patch)

    def music_config(self) -> dict:
        return self._session.media.config()

    def save_music_settings(self, patch: dict, character: dict) -> dict:
        current = self._session.media.config()
        model = MusicSettings.model_validate(current | patch)
        return self._session.media.save_music_settings(model, character)

    def room_context(self) -> dict:
        return self._session.room_context()

    # Live Handoff & History
    def can_accept_handoff(self) -> tuple[bool, str | None]:
        session = self._session
        return session.state in {"live", "paused"}, session.session_id

    def current_session_id(self) -> str | None:
        return self._session.session_id

    async def submit_handoff(self, request_id, session_id, text, *, account):
        return await self._session.submit_handoff(request_id, session_id, text, account=account)

    def handoff_result(self, session_id, job_id) -> dict:
        return self._session.handoff_result(session_id, job_id)

    async def cancel_handoff(self, request_id, session_id):
        return await self._session.cancel_handoff(request_id, session_id)

    async def generate_text_reply(self, event, character, relationship=None):
        return await self._session.generate_text_reply(event, character, relationship)

    def current_live_scene(self) -> LiveScene | None:
        return getattr(self._session, "live_scene", None)

    def recent_activities(self) -> list:
        return list(self._session.recent_activities)

    # Game Livestream & Stardew Device Bridge
    def game_status(self) -> dict:
        return self._session.game_livestream.status()

    def game_adapter_status(self, game_id: str) -> dict:
        adapters = getattr(self._session.game_livestream, "adapters", {})
        if game_id in adapters:
            return adapters[game_id].get_status()
        status = self.game_status()
        if status.get("active_game_id") == game_id:
            return status.get("active_status") or {}
        return {}

    def supported_game_actions(self) -> set | tuple | list:
        return self._session.game_livestream.get_active_adapter().actions

    def action_guard(self) -> dict:
        return self._session.game_livestream.action_guard()

    def validate_game_handoff(self, game_id: str, action: str, *, live_enabled: bool = True) -> dict:
        game = self._session.game_livestream
        state = game.status()
        if not live_enabled:
            raise ValueError("请先启用游戏工作台")
        if not state["is_active"] or state["owner"] != "studio" or game_id != state["active_game_id"]:
            raise ValueError("模拟游戏未激活或已由号主接管")
        if action != "step" and action not in game.get_active_adapter().actions:
            raise ValueError("当前游戏不支持这个动作")
        return {
            "target": state["active_status"]["display_name"] + "（模拟）",
            "guard": game.action_guard(),
        }

    def execute_game_handoff(self, request_id: str, game_id: str, action: str) -> dict:
        return self._session.game_livestream.handoff(request_id, game_id, action)

    def game_handoff_receipt(self, request_id: str, game_id: str, action: str) -> dict | None:
        return self._session.game_livestream.handoff_receipt(request_id, game_id, action)

    def manual_game_action(self, action: str, params: dict | None = None) -> dict:
        return self._session.game_livestream.manual_action(action, params)

    def switch_game(self, target_game_id: str, *, reason: str = "主播后台手动切换") -> dict:
        return self._session.game_livestream.switch_game(target_game_id, reason=reason)

    def transfer_game_control(self, owner: str) -> dict:
        return self._session.game_livestream.transfer_control(owner)

    def step_game(self) -> dict:
        return self._session.game_livestream.step_game()

    def stardew_status(self) -> dict:
        device = self._stardew_device
        if device is None:
            return {"connected": False}
        return device.status()

    def stardew_receipt(self, session, identity) -> dict | None:
        device = self._stardew_device
        return device.receipt(session, identity) if device is not None else None

    def stardew_submit(self, command: DeviceCommand) -> dict:
        if self._stardew_device is None:
            raise ValueError("实机通信尚未初始化")
        return self._stardew_device.submit(command)

    def stardew_cancel(self, session, identity) -> dict | None:
        device = self._stardew_device
        if device is None:
            return None
        return device.cancel(session, identity)


def create_live_facade(session: LiveSession, *, stardew_device: StardewDevice | None = None) -> LiveFacade:
    return LiveFacade(session, stardew_device=stardew_device)


def live_facade(target) -> LiveFacade:
    """Resolve a LiveFacade from a Runtime, LiveFacade or LiveSession instance."""
    if isinstance(target, LiveFacade):
        return target
    if getattr(target, "live_facade", None) is not None:
        return target.live_facade
    if getattr(target, "live", None) is not None:
        return LiveFacade(target.live)
    return LiveFacade(target)
