"""Online selection, bounded preparation and cache; playback stays in Runtime."""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from typing import Literal

import httpx
from pydantic import Field, field_validator

from .live_media import MAX_UPLOAD, StrictModel, inspect_audio
from .models import Event
from .song_component import SongComponent
from .song_download import download


class SongRequestSettings(StrictModel):
    enabled: bool = False
    sources: list[Literal["bodian", "migu"]] = Field(
        default_factory=lambda: ["bodian", "migu"], min_length=1, max_length=2
    )
    quality: Literal["flac_preferred", "high_bitrate"] = "flac_preferred"
    fallback_bitrate: int = Field(default=320000, ge=128000, le=320000)
    allow_lower_quality: bool = False
    cache_limit_mib: int = Field(default=15360, ge=96, le=15360)
    max_download_mib: int = Field(default=96, ge=1, le=96)
    max_duration_seconds: int = Field(default=600, ge=10, le=600)
    license_note: str = Field(default="", max_length=500)
    live_allowed: bool = False
    commercial_allowed: bool = False

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("音源不可重复")
        return value


class OnlineSong(StrictModel):
    id: str = Field(pattern=r"^\d{1,20}$")
    title: str = Field(min_length=1, max_length=100)
    artist: str = Field(min_length=1, max_length=200)
    artists: list[str] = Field(min_length=1, max_length=20)
    album: str = Field(default="", max_length=200)
    cover: str = Field(default="", max_length=2048)
    duration: float = Field(gt=0, le=600)
    aliases: list[str] = Field(default_factory=list, max_length=20)


def normalized(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def cache_bytes(index, identities=None):
    return sum(
        {
            row["audio"]: row["bytes"]
            for row in index.values()
            if identities is None or row["audio"] in identities
        }.values()
    )


def parse_lrc(text):
    result = {}
    offset = re.search(r"\[offset:([+-]?\d+)\]", text, re.I)
    shift = int(offset[1]) / 1000 if offset else 0
    for line in text.splitlines():
        stamps = list(re.finditer(r"\[(\d{1,2}):(\d{2}(?:\.\d{1,3})?)\]", line))
        content = re.sub(r"\[[^]]*\]", "", line).strip()[:200]
        for stamp in stamps:
            moment = int(stamp[1]) * 60 + float(stamp[2]) + shift
            if 0 <= moment <= 600 and float(stamp[2]) < 60:
                result[moment] = content
    return [{"time": moment, "text": result[moment]} for moment in sorted(result)[:1000]]


class OnlineSongs:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store
        self.component = SongComponent(runtime.settings.root, runtime.settings.runtime)
        self.directory = runtime.settings.runtime / "online_music"
        if self.directory.is_symlink():
            raise ValueError("在线音乐目录不能使用链接")
        self.directory.mkdir(exist_ok=True)
        self.tasks = {}
        self.preparation_lock = asyncio.Lock()
        self.config_lock = asyncio.Lock()
        self.epoch = 0
        self.choices = {}
        self.search_times = {}

    def config(self):
        value = {
            **self.store.get("song_requests", {}),
            "cache_limit_mib": self.runtime.cache.policy()["music_limit_mib"],
        }
        return SongRequestSettings.model_validate(value).model_dump()

    def status(self):
        return {
            "settings": self.config(),
            "component": self.component.status(),
            "cache_bytes": self.runtime.cache.usage("music")["bytes"],
            "preparing": [identity for identity, task in self.tasks.items() if not task.done()],
            "candidates": [value for value in self.choices.values() if value["expires_at"] > time.time()],
        }

    async def open(self):
        if self.config()["enabled"]:
            with contextlib.suppress(ValueError):
                await self.component.start()

    async def configure(self, value, *, restart=False):
        config = SongRequestSettings.model_validate(value).model_dump()
        async with self.config_lock:
            cache_policy = self.runtime.cache.policy()
            if cache_policy["music_limit_mib"] != config["cache_limit_mib"]:
                await self.runtime.cache.configure(
                    {**cache_policy, "music_limit_mib": config["cache_limit_mib"]}
                )
            if (
                cache_bytes(self.store.get("online_music_cache", {}), self.protected())
                > config["cache_limit_mib"] * 1024 * 1024
            ):
                raise ValueError("缓存上限低于正在使用的音频容量，请先停止歌曲或移除本地曲库引用")
            previous = self.config()
            changed = previous != config or restart
            if changed:
                self.epoch += 1
                # Gate new requests before yielding to cancellation or subprocess IO.
                self.store.set("song_requests", {**config, "enabled": False})
                self.choices.clear()
                await self.cancel_preparation()
                self.runtime.clear_online_queue()
                if self.runtime.current_is_online():
                    await self.runtime.cancel_current("关闭／重新配置在线点歌组件")
                await self.component.stop()
            if config["enabled"]:
                try:
                    await self.component.start()
                except ValueError:
                    # Persist the requested switch with its concrete failure state.
                    self.store.set("song_requests", config)
                    return self.status()
            self.store.set("song_requests", config)
            if not config["enabled"]:
                self.component.error = None
            self.evict()
            self.sync()
            return self.status()

    async def cancel_preparation(self):
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        await self.cancel_preparation()
        await self.component.stop()

    def require_enabled(self):
        if not self.config()["enabled"]:
            raise ValueError("在线点歌未启用")
        if not self.component.status()["running"] or self.component.error:
            raise ValueError(self.component.error or "在线点歌组件未启动，可点击重启")

    async def search(self, query):
        self.require_enabled()
        epoch = self.epoch
        response = await self.component.call("search", {"query": query})
        self.require_enabled()
        if epoch != self.epoch:
            raise ValueError("搜索期间在线点歌设置已改变，请重试")
        songs = []
        for row in response.get("songs", [])[:20]:
            try:
                songs.append(OnlineSong.model_validate(row).model_dump())
            except ValueError:
                continue
        return songs

    def snapshot(self, song, event):
        self.require_enabled()
        config = self.config()
        if (
            not config["live_allowed"]
            or not config["license_note"].strip()
            or (self.runtime.settings.data.get("commercial_mode") and not config["commercial_allowed"])
        ):
            raise ValueError("请先在在线点歌设置中确认音源的目标用途及许可范围")
        song = OnlineSong.model_validate(song).model_dump()
        if song["duration"] > config["max_duration_seconds"]:
            raise ValueError("歌曲时长超过在线点歌上限")
        return {
            "online": {"id": song["id"], "epoch": self.epoch},
            "song": {**song, "id": "online_" + song["id"], "lyrics": []},
            "mode": "direct",
            "duration": song["duration"],
            "requester": event.display_name,
            "voice_id": self.runtime.character["voice_profile"],
            "ttl": self.runtime.media.config()["request_ttl_seconds"],
        }

    async def selection(self, identity, event):
        self.require_enabled()
        if event.metadata.get("mode", "direct") != "direct":
            raise ValueError("在线点歌当前只支持直接播放，演唱模型另行挂载")
        epoch = self.epoch
        session = self.runtime.session_id
        row = await self.component.call("detail", {"id": identity})
        if epoch != self.epoch or session != self.runtime.session_id:
            raise ValueError("选歌期间在线点歌设置已改变，请重试")
        return self.snapshot(row, event)

    async def interpret(self, event):
        """Return a snapshot or confirmation; None means ordinary chat/TTS."""
        if not self.config()["enabled"]:
            return None
        command = re.fullmatch(r"\s*点歌\s+(.{1,100}?)\s*", event.text)
        number = re.fullmatch(r"\s*(?:选歌\s*)?([1-9]|1\d|20)\s*", event.text)
        owner = [event.platform, event.room_id, event.platform_user_id]
        self.choices = {
            key: value for key, value in self.choices.items() if value["expires_at"] > time.time()
        }
        if number and event.platform_user_id:
            pending = next(
                (value for value in reversed(list(self.choices.values())) if value["owner"] == owner), None
            )
            if pending:
                return self.confirm(pending["token"], int(number[1]), event=event)
        if not command:
            return None
        self.require_enabled()
        key = tuple(owner) if event.platform_user_id else (event.platform, event.room_id, "anonymous")
        previous = self.search_times.get(key, -10000)
        if time.monotonic() - previous < max(2, self.runtime.media.config()["cooldown_seconds"]):
            raise ValueError("同用户点歌搜索短时限流")
        self.search_times[key] = time.monotonic()
        if len(self.search_times) > 2000:
            self.search_times = {
                key: stamp for key, stamp in self.search_times.items() if stamp > time.monotonic() - 3600
            }
            while len(self.search_times) > 2000:
                self.search_times.pop(next(iter(self.search_times)))
        session = self.runtime.session_id
        songs = await self.search(command[1])
        if session != self.runtime.session_id or self.runtime.state == "stopped":
            raise ValueError("搜索期间直播会话已改变，请重新点歌")
        if time.time() - event.timestamp.timestamp() > self.runtime.settings.data["event_ttl_seconds"]:
            raise ValueError("点歌事件已过期，请重新发送")
        if not songs:
            raise ValueError("没有找到歌曲，请补充歌手或版本")
        query = normalized(command[1])
        exact = [
            song
            for song in songs
            if query
            in {
                normalized(song["title"]),
                normalized(song["title"] + song["artist"]),
                normalized(song["artist"] + song["title"]),
            }
        ]
        if len(exact) == 1:
            return self.snapshot(exact[0], event)
        token = secrets.token_urlsafe(18)
        choice = {
            "token": token,
            "owner": owner,
            "requester": event.display_name,
            "songs": (exact or songs)[:10],
            "expires_at": time.time() + 120,
        }
        if len(self.choices) >= 100:
            self.choices.pop(next(iter(self.choices)))
        self.choices[token] = choice
        return {"status": "needs_confirmation", "reason": "请回复序号，或由主播确认歌曲版本", **choice}

    def confirm(self, token, number, *, event=None):
        self.require_enabled()
        choice = self.choices.get(token)
        if not choice or choice["expires_at"] <= time.time():
            raise ValueError("歌曲候选已过期，请重新搜索")
        if not isinstance(number, int) or not 1 <= number <= len(choice["songs"]):
            raise ValueError("候选序号无效")
        if event is None:
            event = Event(
                type="song",
                display_name=choice["requester"],
                platform=choice["owner"][0],
                room_id=choice["owner"][1],
                platform_user_id=choice["owner"][2],
            )
        result = self.snapshot(choice["songs"][number - 1], event)
        result["online"]["choice_token"] = token
        return event, result

    def sync(self):
        active = ([self.runtime.current_id] if self.runtime.current_id else []) + list(self.runtime.queue)
        songs = [(identity, self.store.job(identity)) for identity in active]
        songs = [
            (identity, job)
            for identity, job in songs
            if job
            and job.get("media")
            and job["status"] not in {"completed", "cancelled", "failed", "expired"}
        ][:2]
        desired = {
            identity: json.loads(job["media"])
            for identity, job in songs
            if json.loads(job["media"]).get("online")
        }
        if not self.config()["enabled"] or self.runtime.state == "stopped":
            desired = {}
        for identity in list(self.tasks):
            if identity not in desired:
                self.tasks.pop(identity).cancel()
        for identity, snapshot in desired.items():
            if identity not in self.tasks:
                task = asyncio.create_task(self._prepare(snapshot))
                # Observe prefetch errors even before the live worker selects it.
                task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                self.tasks[identity] = task

    def preparation_status(self, identity):
        task = self.tasks.get(identity)
        if not task:
            return "等待准备", None
        if not task.done():
            return "正在获取音源", None
        if task.cancelled():
            return "已取消", None
        error = task.exception()
        return ("准备失败", str(error)) if error else ("音频已缓存", None)

    async def prepared(self, identity):
        self.sync()
        task = self.tasks.get(identity)
        if not task:
            raise ValueError("在线点歌准备已取消")
        return await asyncio.shield(task)

    def cache_key(self, snapshot, config):
        identity = [
            snapshot["online"]["id"],
            snapshot["song"]["title"],
            snapshot["song"]["artists"],
            snapshot["duration"],
            config["sources"],
            config["quality"],
            config["fallback_bitrate"],
            config["allow_lower_quality"],
        ]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    def protected(self):
        result = {song["audio"] for song in self.runtime.media.songs()}
        active = ([self.runtime.current_id] if self.runtime.current_id else []) + self.runtime.queue
        for identity in active:
            job = self.store.job(identity)
            if job and job.get("media"):
                audio = json.loads(job["media"])["song"].get("audio")
                if audio:
                    result.add(audio)
        for task in self.tasks.values():
            if task.done() and not task.cancelled() and task.exception() is None:
                result.add(task.result()["song"]["audio"])
        return result

    def evict(self, *, reserve=0):
        self.runtime.cache.enforce("music", reserve=reserve)

    async def _prepare(self, snapshot):
        async with self.preparation_lock:
            self.require_enabled()
            if snapshot["online"]["epoch"] != self.epoch:
                raise ValueError("在线点歌设置已改变，请重新点歌")
            config = self.config()
            key = self.cache_key(snapshot, config)
            index = self.store.get("online_music_cache", {})
            cached = index.get(key)
            if cached:
                try:
                    path = self.runtime.media.blob(cached["audio"])
                    if (
                        await asyncio.to_thread(lambda: hashlib.sha256(path.read_bytes()).hexdigest())
                        != cached["audio"]
                    ):
                        raise ValueError("缓存内容改变")
                    info = await asyncio.to_thread(inspect_audio, path)
                    self.validate_quality(info, snapshot, config)
                    cached["used_at"] = time.time()
                    self.store.set("online_music_cache", index)
                    return self.materialize(snapshot, cached)
                except (ValueError, OSError):
                    index.pop(key, None)
                    self.store.set("online_music_cache", index)
            lyric = await self.component.call("lyrics", {"id": snapshot["online"]["id"]})
            maximum = min(MAX_UPLOAD, config["max_download_mib"] * 1024 * 1024)
            temporary = self.directory / (".download-" + secrets.token_hex(16))
            errors = []
            try:
                for source in config["sources"]:
                    for flac in [True, False] if config["quality"] == "flac_preferred" else [False]:
                        try:
                            resolved = await self.component.call(
                                "resolve", {"id": snapshot["online"]["id"], "source": source, "flac": flac}
                            )
                            actual = OnlineSong.model_validate(resolved["song"]).model_dump()
                            expected = snapshot["song"]
                            if (
                                actual["title"] != expected["title"]
                                or actual["artists"] != expected["artists"]
                                or abs(actual["duration"] - snapshot["duration"]) > 3
                            ):
                                raise ValueError("歌曲版本信息在排队后改变，请重新确认")
                            async with self.runtime.cache.write("music", maximum) as allowed:
                                identity, size = await download(resolved["url"], temporary, allowed)
                                self.runtime.cache.touch(temporary, seconds=300)
                            info = await asyncio.to_thread(inspect_audio, temporary)
                            self.validate_quality(info, snapshot, config)
                            self.require_enabled()
                            if snapshot["online"]["epoch"] != self.epoch:
                                raise ValueError("在线点歌准备已取消")
                            target = self.directory / (identity + "." + info["format"])
                            if target.is_symlink():
                                raise ValueError("在线缓存文件不能使用链接")
                            os.replace(temporary, target)
                            row = {
                                "audio": identity,
                                "bytes": size,
                                "quality": info,
                                "source": source,
                                "lyrics": parse_lrc(lyric.get("lrc", "")),
                                "lyric_warning": lyric.get("warning"),
                                "used_at": time.time(),
                            }
                            index = self.store.get("online_music_cache", {})
                            index[key] = row
                            self.store.set("online_music_cache", index)
                            return self.materialize(snapshot, row)
                        except httpx.HTTPStatusError as error:
                            errors.append(
                                f"{source} {'FLAC' if flac else '高码率'}：下载返回 HTTP {error.response.status_code}"
                            )
                            temporary.unlink(missing_ok=True)
                        except httpx.TimeoutException:
                            errors.append(f"{source}：音源下载连接／读取超时")
                            temporary.unlink(missing_ok=True)
                        except (ValueError, OSError, TimeoutError) as error:
                            errors.append(f"{source} {'FLAC' if flac else '高码率'}：{str(error)[:200]}")
                            temporary.unlink(missing_ok=True)
                        except Exception as error:
                            errors.append(f"{source}：{type(error).__name__}")
                            temporary.unlink(missing_ok=True)
                raise ValueError("；".join(errors))
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def validate_quality(info, snapshot, config):
        if info["format"] not in {"flac", "mp3"}:
            raise ValueError("在线音源仅接受真实 FLAC／MP3")
        if (
            abs(info["duration"] - snapshot["duration"]) > 3
            or info["duration"] > config["max_duration_seconds"]
        ):
            raise ValueError("音源实际时长与确认版本不一致")
        if (
            info["format"] == "mp3"
            and not config["allow_lower_quality"]
            and info["bitrate"] < config["fallback_bitrate"] * 0.98
        ):
            raise ValueError(f"实际码率 {info['bitrate']} 低于要求 {config['fallback_bitrate']}")

    @staticmethod
    def materialize(snapshot, cached):
        return {
            **snapshot,
            "duration": cached["quality"]["duration"],
            "song": {
                **snapshot["song"],
                "audio": cached["audio"],
                "source": cached["source"],
                "quality": cached["quality"],
                "lyrics": cached["lyrics"],
                "lyric_warning": cached.get("lyric_warning"),
            },
        }
