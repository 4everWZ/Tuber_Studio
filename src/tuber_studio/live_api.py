"""Authenticated live authoring and playback-only scene projections."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from fastapi import Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .live_media import MAX_UPLOAD, MusicSettings, default_scene, inspect_wav


def register_live_api(app, settings, runtime, require_admin, admin_ok, overlay_ok):
    media, store = runtime.media, runtime.store

    async def require_view(request: Request):
        if not (admin_ok(request) or overlay_ok(request)):
            raise HTTPException(401, "需要管理或播放器凭证")

    def view():
        queued = []
        for identity in runtime.queue:
            job = store.job(identity)
            event = store.event(job["event_id"])["payload"]
            snapshot = json.loads(job["media"]) if job.get("media") else None
            if snapshot:
                snapshot = {
                    "mode": snapshot["mode"],
                    "song": {key: snapshot["song"][key] for key in ("id", "title", "artist")},
                }
            queued.append(
                {
                    "id": identity,
                    "type": event["type"],
                    "text": event["text"],
                    "requester": event["display_name"],
                    "media": snapshot,
                }
            )
        events = [
            json.loads(row["payload"])
            for row in store.all(
                "SELECT payload FROM events WHERE session_id=? ORDER BY id DESC LIMIT 20",
                (runtime.session_id,),
            )
        ]
        character = runtime.character
        return {
            "scene": media.scene(),
            "queue": queued,
            "events": list(reversed(events)),
            "character": {key: character[key] for key in ("id", "name", "version")},
            "assets": settings.assets(character),
            "playback": runtime.playback,
            "state": runtime.state,
            "muted": runtime.muted,
            "server_time": time.time(),
            "overlay_connected": runtime.overlay is not None,
        }

    @app.get("/live")
    async def workbench():
        return FileResponse(Path(__file__).parent / "web/live.html")

    @app.get("/api/live/view", dependencies=[Depends(require_view)])
    async def live_view():
        return view()

    @app.put("/api/live/scene", dependencies=[Depends(require_admin)])
    async def save_scene(data: dict = Body(...)):
        return media.save_scene(data)

    @app.get("/api/live/scene/default", dependencies=[Depends(require_admin)])
    async def reset_scene():
        return default_scene()

    @app.get("/api/live/music", dependencies=[Depends(require_admin)])
    async def music():
        return {
            "settings": media.config(),
            "songs": media.songs(),
            "singing": media.singing.describe(runtime.character),
        }

    @app.put("/api/live/music", dependencies=[Depends(require_admin)])
    async def save_music(data: MusicSettings):
        if data.default_mode == "sing" and not media.singing.describe(runtime.character)["available"]:
            raise ValueError("演唱模型尚未接入，当前点歌使用直接播放")
        store.set("music_settings", data.model_dump())
        return media.config()

    @app.put("/api/live/songs/{identity}", dependencies=[Depends(require_admin)])
    async def save_song(identity: str, data: dict = Body(...)):
        if data.get("id") != identity:
            raise ValueError("歌曲ID不一致")
        return await media.save_song(data)

    @app.delete("/api/live/songs/{identity}", dependencies=[Depends(require_admin)])
    async def remove_song(identity: str):
        store.set("music_catalog", [song for song in media.songs() if song["id"] != identity])
        return {"removed": identity}

    @app.post("/api/live/uploads", dependencies=[Depends(require_admin)])
    async def upload(request: Request):
        descriptor, name = tempfile.mkstemp(prefix=".upload-", dir=media.directory)
        temporary = Path(name)
        size, digest = 0, hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "WAV上限96MiB")
                    digest.update(chunk)
                    output.write(chunk)
            info = await asyncio.to_thread(inspect_wav, temporary)
            identity = digest.hexdigest()
            target = media.directory / (identity + ".wav")
            if target.is_symlink():
                raise ValueError("音乐文件不能使用链接")
            if not target.exists():
                os.replace(temporary, target)
            return {"id": identity, "duration": info["duration"], "bytes": size}
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/media/music/{identity}", dependencies=[Depends(require_view)])
    async def music_file(identity: str):
        return FileResponse(media.blob(identity), media_type="audio/wav")

    @app.put("/api/live/queue", dependencies=[Depends(require_admin)])
    async def reorder(data: list[str] = Body(...)):
        if len(data) != len(set(data)) or set(data) != set(runtime.queue):
            raise ValueError("队列已改变，请刷新后重排完整待播队列")
        runtime.queue[:] = data
        return {"queue": data}

    @app.delete("/api/live/queue/{identity}", dependencies=[Depends(require_admin)])
    async def remove_queued(identity: str):
        if identity not in runtime.queue:
            raise ValueError("该任务已开始或不在队列中")
        runtime.queue.remove(identity)
        job = store.job(identity)
        store.update_job(identity, status="cancelled", error="人工移除待播任务")
        store.execute(
            "UPDATE events SET status='cancelled',reason=? WHERE id=?", ("人工移除", job["event_id"])
        )
        return {"removed": identity}
