"""Optional singing model boundary. No singing model is loaded by default."""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import atomic_json
from .live_media import inspect_wav
from .providers import Audio


@dataclass(frozen=True)
class SingingRequest:
    song_id: str
    title: str
    audio: Path
    vocal: Path | None
    accompaniment: Path | None
    lyrics: tuple
    voice: dict
    reference: Path | None
    output_directory: Path


@dataclass(frozen=True)
class SingingResult:
    audio: Path
    voice_id: str
    provider_id: str
    provider_version: str
    vocal: Path | None = None


class SingingProvider(Protocol):
    """A mounted model owns inference and cleanup on cancellation, never playback."""

    id: str
    version: str
    timeout_seconds: float

    async def prepare(self, request: SingingRequest) -> SingingResult: ...
    async def close(self) -> None: ...


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class SingingService:
    def __init__(self, settings, media, provider: SingingProvider | None = None):
        self.settings, self.media, self.provider = settings, media, provider
        self.root = settings.runtime / "singing"
        if provider is not None and (
            not isinstance(provider.id, str)
            or not provider.id
            or not isinstance(provider.version, str)
            or not provider.version
            or not 0 < provider.timeout_seconds <= 3600
        ):
            raise ValueError("演唱模型须声明ID、版本和有界准备超时")

    def describe(self, character):
        return {
            "available": self.provider is not None,
            "reason": "演唱音频模型待接入；当前点歌直接播放"
            if self.provider is None
            else "已挂载演唱模型；声音效果与设备资源须单独验收",
            "provider": self.provider.id if self.provider else None,
            "version": self.provider.version if self.provider else None,
            "voice_id": character["voice_profile"],
        }

    async def close(self):
        if self.provider is not None:
            await self.provider.close()

    def reference(self, voice):
        return self.settings.voice_reference_path(voice) if voice.get("provider") == "gpt_sovits" else None

    def identity(self, character):
        if self.provider is None:
            raise ValueError(self.describe(character)["reason"])
        voice = self.settings.voice(character["voice_profile"])
        reference = self.reference(voice)
        return {
            "provider": self.provider.id,
            "version": self.provider.version,
            "voice_id": voice["id"],
            "voice_version": voice.get("version"),
            "voice_profile_sha256": hashlib.sha256(json.dumps(voice, sort_keys=True).encode()).hexdigest(),
            "reference_sha256": digest(reference) if reference else None,
        }

    def publish(self, source):
        source = Path(source)
        if source.is_symlink() or not source.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("演唱输出须位于受控runtime/singing目录且不能使用链接")
        inspect_wav(source)
        identity = digest(source)
        target = self.media.directory / (identity + ".wav")
        if target.exists():
            if target.is_symlink() or digest(target) != identity:
                raise ValueError("演唱缓存内容身份失效")
            return identity
        descriptor, name = tempfile.mkstemp(prefix=".singing-", dir=self.media.directory)
        os.close(descriptor)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return identity

    async def prepare(self, snapshot, character):
        identity = self.identity(character)
        if identity != snapshot.get("singing"):
            raise ValueError("排队后声音或演唱模型身份已改变，请重新点歌")
        song = snapshot["song"]
        paths = {}
        for field in ("audio", "vocal", "accompaniment"):
            path = self.media.blob(song[field]) if song.get(field) else None
            if path and await asyncio.to_thread(digest, path) != song[field]:
                raise ValueError("歌曲或引导素材内容已改变，请重新导入")
            paths[field] = path
        key = hashlib.sha256(
            json.dumps({"identity": identity, "song": song}, sort_keys=True).encode()
        ).hexdigest()
        cache = self.root / "cache" / (key + ".json")
        if cache.is_file():
            result = json.loads(cache.read_text(encoding="utf-8"))
        else:
            output = self.root / "output" / key
            output.mkdir(parents=True, exist_ok=True)
            voice = self.settings.voice(identity["voice_id"])
            request = SingingRequest(
                song["id"],
                song["title"],
                paths["audio"],
                paths["vocal"],
                paths["accompaniment"],
                tuple(song["lyrics"]),
                voice,
                self.reference(voice),
                output,
            )
            try:
                prepared = await asyncio.wait_for(
                    self.provider.prepare(request), self.provider.timeout_seconds
                )
            except TimeoutError:
                raise ValueError("演唱模型准备超时，未播放任何替代音源") from None
            if not isinstance(prepared, SingingResult) or (
                prepared.voice_id != identity["voice_id"]
                or prepared.provider_id != identity["provider"]
                or prepared.provider_version != identity["version"]
            ):
                raise ValueError("演唱输出的声音或模型身份不匹配")
            result = {
                "audio": await asyncio.to_thread(self.publish, prepared.audio),
                "vocal": await asyncio.to_thread(self.publish, prepared.vocal) if prepared.vocal else None,
            }
            atomic_json(cache, result)
        audio = self.media.blob(result["audio"])
        if await asyncio.to_thread(digest, audio) != result["audio"]:
            raise ValueError("演唱缓存内容身份失效")
        info = await asyncio.to_thread(inspect_wav, audio)
        activity = None
        if result.get("vocal"):
            vocal = self.media.blob(result["vocal"])
            if await asyncio.to_thread(digest, vocal) != result["vocal"]:
                raise ValueError("演唱人声缓存内容身份失效")
            activity = await asyncio.to_thread(inspect_wav, vocal, envelope=True)
        return Audio(audio, info["duration"], song["title"]), activity
