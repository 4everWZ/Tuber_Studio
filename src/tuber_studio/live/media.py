"""Operator-owned scene, licensed music catalog and immutable request snapshots."""

import asyncio
import hashlib
import re
from typing import Literal

from pydantic import Field

from ..core.models import StrictModel
from ..infra.inference.providers import Audio
from .music.audio import MAX_UPLOAD as MAX_UPLOAD
from .music.audio import MEDIA_TYPES as MEDIA_TYPES
from .music.audio import inspect_audio as inspect_audio
from .music.audio import inspect_wav as inspect_wav

BLOB = re.compile(r"^[a-f0-9]{64}$")


class SceneItem(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    kind: Literal["avatar", "title", "now", "lyrics", "queue", "chat", "gifts", "decoration"]
    x: float = Field(ge=0, le=3840)
    y: float = Field(ge=0, le=2160)
    width: float = Field(ge=40, le=3840)
    height: float = Field(ge=30, le=2160)
    z: int = Field(default=1, ge=0, le=100)
    visible: bool = True
    locked: bool = False
    text: str = Field(default="", max_length=120)


class Scene(StrictModel):
    width: int = Field(default=1920, ge=640, le=3840)
    height: int = Field(default=1080, ge=360, le=2160)
    title: str = Field(default="直播节目", max_length=80)
    background: Literal["ocean", "transparent"] = "ocean"
    items: list[SceneItem] = Field(min_length=1, max_length=32)


class MusicSettings(StrictModel):
    default_mode: Literal["direct", "sing"] = "direct"
    volume: float = Field(default=0.8, ge=0, le=1)
    queue_limit: int = Field(default=20, ge=1, le=100)
    cooldown_seconds: int = Field(default=30, ge=0, le=3600)
    request_ttl_seconds: int = Field(default=3600, ge=60, le=7200)


class Lyric(StrictModel):
    time: float = Field(ge=0, le=600)
    text: str = Field(max_length=200)


class Song(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,60}$")
    title: str = Field(min_length=1, max_length=100)
    artist: str = Field(default="", max_length=100)
    audio: str = Field(pattern=r"^[a-f0-9]{64}$")
    vocal: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    accompaniment: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    license_note: str = Field(min_length=1, max_length=500)
    live_allowed: bool = False
    commercial_allowed: bool = False
    lyrics: list[Lyric] = Field(default_factory=list, max_length=1000)


ObsLayoutId = Literal["chat", "game"]
VALID_LAYOUTS: tuple[ObsLayoutId, ...] = ("chat", "game")


def default_scene():
    entries = [
        ("title", "title", 60, 35, 1150, 135, ""),
        ("avatar", "avatar", 220, 120, 900, 800, ""),
        ("now", "now", 1330, 180, 530, 245, ""),
        ("queue", "queue", 1330, 450, 530, 555, ""),
        ("lyrics", "lyrics", 160, 795, 1070, 80, ""),
        ("chat", "chat", 60, 955, 1200, 95, ""),
        ("gifts", "gifts", 60, 880, 430, 65, ""),
        ("shell", "decoration", 1130, 670, 100, 100, "🐚"),
        ("microphone", "decoration", 935, 585, 115, 180, "🎙️"),
    ]
    return Scene(
        background="ocean",
        items=[
            SceneItem(id=i, kind=k, x=x, y=y, width=w, height=h, text=t) for i, k, x, y, w, h, t in entries
        ],
    ).model_dump()


def default_game_scene():
    """Compact bottom-right PiP transparent layout leaving ~75% left/center clear for games/shows."""
    entries = [
        ("title", "title", 32, 24, 520, 60, "", True),
        ("now", "now", 32, 92, 460, 64, "", True),
        ("avatar", "avatar", 1440, 620, 440, 420, "", True),
        ("lyrics", "lyrics", 320, 885, 1040, 70, "", True),
        ("chat", "chat", 320, 965, 1040, 85, "", True),
        ("gifts", "gifts", 32, 975, 260, 75, "", True),
        ("queue", "queue", 1440, 24, 440, 240, "", False),
    ]
    return Scene(
        title="游戏节目",
        background="transparent",
        items=[
            SceneItem(id=i, kind=k, x=x, y=y, width=w, height=h, text=t, visible=v)
            for i, k, x, y, w, h, t, v in entries
        ],
    ).model_dump()


def _normalize_layout(layout: str) -> ObsLayoutId:
    if layout not in VALID_LAYOUTS:
        raise ValueError("未知场景布局，仅支持 chat 或 game")
    return layout  # type: ignore[return-value]


def _default_for_layout(layout: ObsLayoutId) -> dict:
    return default_game_scene() if layout == "game" else default_scene()


def _validate_scene_dict(value) -> dict:
    scene = Scene.model_validate(value).model_dump()
    ids = [item["id"] for item in scene["items"]]
    if len(ids) != len(set(ids)):
        raise ValueError("场景组件ID不可重复")
    for item in scene["items"]:
        if item["x"] + item["width"] > scene["width"] or item["y"] + item["height"] > scene["height"]:
            raise ValueError("组件须位于画布范围内")
    return scene


class LiveMedia:
    def __init__(self, settings, store, *, singing_provider=None):
        self.settings, self.store = settings, store
        self.directory = settings.runtime / "music"
        if self.directory.is_symlink():
            raise ValueError("音乐目录不能使用链接")
        self.directory.mkdir(exist_ok=True)
        from .music.singing import SingingService

        self.singing = SingingService(settings, self, singing_provider)

    def blob(self, identity):
        if not isinstance(identity, str) or not BLOB.fullmatch(identity):
            raise ValueError("未知音乐素材ID")
        for directory in (self.directory, self.settings.runtime / "online_music"):
            if directory.is_symlink():
                raise ValueError("音乐目录不能使用链接")
            for suffix in MEDIA_TYPES:
                path = directory / (identity + suffix)
                if path.is_symlink():
                    raise ValueError("音乐文件不能使用链接")
                if path.is_file() and path.resolve().parent == directory.resolve():
                    return path
        raise ValueError("音乐素材不存在")

    def config(self):
        return MusicSettings.model_validate(self.store.get("music_settings", {})).model_dump()

    def save_music_settings(self, data, character):
        if data.default_mode == "sing" and not self.singing.describe(character)["available"]:
            raise ValueError("演唱模型尚未接入，当前点歌使用直接播放")
        self.store.set("music_settings", data.model_dump())
        return self.config()

    def layout_state(self) -> dict:
        raw = self.store.get("live_scene_layout", {})
        if not isinstance(raw, dict):
            raw = {}
        active = raw.get("active_layout")
        return {
            "active_layout": active if active in VALID_LAYOUTS else "chat",
            "layout_locked": bool(raw.get("layout_locked", False)),
        }

    def set_layout(self, layout: str | None = None, locked: bool | None = None) -> dict:
        state = self.layout_state()
        if layout is not None:
            state["active_layout"] = _normalize_layout(layout)
        if locked is not None:
            state["layout_locked"] = bool(locked)
        self.store.set("live_scene_layout", state)
        return state

    def _profiles(self) -> dict[str, dict]:
        raw = self.store.get("live_scene_profiles")
        if isinstance(raw, dict) and "chat" in raw and "game" in raw:
            return {"chat": raw["chat"], "game": raw["game"]}
        chat_scene = (raw.get("chat") if isinstance(raw, dict) else None) or self.store.get("live_scene") or default_scene()
        game_scene = (raw.get("game") if isinstance(raw, dict) else None) or default_game_scene()
        return {"chat": chat_scene, "game": game_scene}

    def scene_for(self, layout: str) -> dict:
        target = _normalize_layout(layout)
        return self._profiles()[target]

    def save_scene_for(self, layout: str, value) -> dict:
        target = _normalize_layout(layout)
        scene = _validate_scene_dict(value)
        profiles = self._profiles()
        profiles[target] = scene
        self.store.set("live_scene_profiles", profiles)
        if target == self.layout_state()["active_layout"]:
            self.store.set("live_scene", scene)
        return scene

    def scene_profiles(self) -> dict:
        ls = self.layout_state()
        return {
            "active_layout": ls["active_layout"],
            "layout_locked": ls["layout_locked"],
            "profiles": self._profiles(),
            "defaults": {"chat": default_scene(), "game": default_game_scene()},
        }

    def scene(self, layout: str | None = None):
        target = _normalize_layout(layout) if layout is not None else self.layout_state()["active_layout"]
        return self.scene_for(target)

    def save_scene(self, value, layout: str | None = None):
        target = _normalize_layout(layout) if layout is not None else self.layout_state()["active_layout"]
        return self.save_scene_for(target, value)

    def songs(self):
        return self.store.get("music_catalog", [])

    async def save_song(self, value):
        song = Song.model_validate(value).model_dump()
        for field in ("audio", "vocal", "accompaniment"):
            if song[field]:
                inspector = inspect_audio if field == "audio" else inspect_wav
                await asyncio.to_thread(inspector, self.blob(song[field]))
        times = [line["time"] for line in song["lyrics"]]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("歌词时间须严格递增")
        songs = [entry for entry in self.songs() if entry["id"] != song["id"]]
        if len(songs) >= 200:
            raise ValueError("曲库上限200首")
        self.store.set("music_catalog", songs + [song])
        if getattr(self, "cache", None):
            self.cache.release_uploads(song)
        return song

    def request(self, event, character):
        config = self.config()
        requested = event.metadata.get("song_id")
        candidates = (
            [s for s in self.songs() if s["id"] == requested]
            if requested
            else [s for s in self.songs() if s["title"].casefold() == event.text.strip().casefold()]
        )
        if len(candidates) != 1:
            raise ValueError("请从曲库确认具体歌曲；没有匹配或存在同名候选")
        song = candidates[0]
        if not song["live_allowed"] or (
            self.settings.data.get("commercial_mode") and not song["commercial_allowed"]
        ):
            raise ValueError("歌曲尚未确认相应用途许可")
        mode = event.metadata.get("mode", config["default_mode"])
        if not isinstance(mode, str) or mode not in {"direct", "sing"}:
            raise ValueError("未知点歌模式")
        if mode == "sing":
            capability = self.singing.describe(character)
            if not capability["available"]:
                raise ValueError(capability["reason"])
        path = self.blob(song["audio"])
        info = inspect_audio(path, scan=False)
        snapshot = {
            "song": song,
            "mode": mode,
            "voice_id": character["voice_profile"],
            "duration": info["duration"],
            "requester": event.display_name,
            "ttl": config["request_ttl_seconds"],
        }
        if mode == "sing":
            snapshot["singing"] = self.singing.identity(character)
        return snapshot

    def prepare(self, snapshot):
        song = snapshot["song"]
        path = self.blob(song["audio"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != song["audio"]:
            raise ValueError("歌曲音频内容已改变，请重新导入")
        info = inspect_audio(path)
        activity = inspect_wav(self.blob(song["vocal"]), envelope=True) if song.get("vocal") else None
        return Audio(path, info["duration"], song["title"]), activity
