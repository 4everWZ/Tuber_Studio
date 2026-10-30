"""Operator-owned scene, licensed music catalog and immutable request snapshots."""

import array
import asyncio
import hashlib
import math
import re
import sys
import wave
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .providers import Audio

MAX_UPLOAD = 96 * 1024 * 1024
BLOB = re.compile(r"^[a-f0-9]{64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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
    title: str = Field(default="大肥鱼的深海电台", max_length=80)
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
        items=[
            SceneItem(id=i, kind=k, x=x, y=y, width=w, height=h, text=t) for i, k, x, y, w, h, t in entries
        ]
    ).model_dump()


def inspect_wav(path, *, envelope=False, scan=True):
    """Bounded PCM validation; envelope uses the isolated vocal, never accompaniment."""
    try:
        with wave.open(str(path), "rb") as stream:
            channels, width, rate, count = (
                stream.getnchannels(),
                stream.getsampwidth(),
                stream.getframerate(),
                stream.getnframes(),
            )
            duration = count / rate
            if (
                channels not in {1, 2}
                or width != 2
                or not 8000 <= rate <= 96000
                or not 0.05 <= duration <= 600
            ):
                raise ValueError("请使用0.05至600秒、单／双声道的16-bit PCM WAV")
            if not scan:
                return {"duration": duration, "envelope": [], "step": 0.05}
            values, peak = [], 0
            frames = max(1, round(rate * 0.05))
            actual = 0
            while chunk := stream.readframes(frames):
                samples = array.array("h", chunk)
                if sys.byteorder != "little":
                    samples.byteswap()
                actual += len(chunk)
                level = max((abs(v) for v in samples), default=0)
                peak = max(peak, level)
                if envelope:
                    values.append(round(math.sqrt(sum(v * v for v in samples) / len(samples)) / 32768, 4))
            if actual != count * channels * width or peak < 20:
                raise ValueError("音频不完整或全静音")
            return {"duration": duration, "envelope": values, "step": 0.05}
    except (wave.Error, EOFError, ZeroDivisionError, OSError):
        raise ValueError("音频须为完整有效的16-bit PCM WAV") from None


class LiveMedia:
    def __init__(self, settings, store, *, singing_provider=None):
        self.settings, self.store = settings, store
        self.directory = settings.runtime / "music"
        if self.directory.is_symlink():
            raise ValueError("音乐目录不能使用链接")
        self.directory.mkdir(exist_ok=True)
        from .singing import SingingService

        self.singing = SingingService(settings, self, singing_provider)

    def blob(self, identity):
        if not isinstance(identity, str) or not BLOB.fullmatch(identity):
            raise ValueError("未知音乐素材ID")
        path = self.directory / (identity + ".wav")
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.directory.resolve():
            raise ValueError("音乐素材不存在")
        return path

    def config(self):
        return MusicSettings.model_validate(self.store.get("music_settings", {})).model_dump()

    def scene(self):
        return self.store.get("live_scene", default_scene())

    def save_scene(self, value):
        scene = Scene.model_validate(value).model_dump()
        ids = [item["id"] for item in scene["items"]]
        if len(ids) != len(set(ids)):
            raise ValueError("场景组件ID不可重复")
        for item in scene["items"]:
            if item["x"] + item["width"] > scene["width"] or item["y"] + item["height"] > scene["height"]:
                raise ValueError("组件须位于画布范围内")
        self.store.set("live_scene", scene)
        return scene

    def songs(self):
        return self.store.get("music_catalog", [])

    async def save_song(self, value):
        song = Song.model_validate(value).model_dump()
        for field in ("audio", "vocal", "accompaniment"):
            if song[field]:
                await asyncio.to_thread(inspect_wav, self.blob(song[field]))
        times = [line["time"] for line in song["lyrics"]]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("歌词时间须严格递增")
        songs = [entry for entry in self.songs() if entry["id"] != song["id"]]
        if len(songs) >= 200:
            raise ValueError("曲库上限200首")
        self.store.set("music_catalog", songs + [song])
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
        info = inspect_wav(path, scan=False)
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
        info = inspect_wav(path)
        activity = inspect_wav(self.blob(song["vocal"]), envelope=True) if song.get("vocal") else None
        return Audio(path, info["duration"], song["title"]), activity
