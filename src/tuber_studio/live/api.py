"""Authenticated live authoring and playback-only scene projections."""

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from fastapi import Body, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import Field

from ..media_cache import CachePolicy
from ..models import Event, StrictModel
from .activity_filter import LivePoll
from .avatar import LiveAvatar
from .media import MAX_UPLOAD, MEDIA_TYPES, MusicSettings, default_scene, inspect_audio
from .online_songs import SongRequestSettings
from .options import LiveOptions
from .studio import room_feed


class ConfirmSong(StrictModel):
    token: str = Field(min_length=20, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    number: int = Field(ge=1, le=20, strict=True)


def register_live_api(app, settings, runtime, require_admin, admin_ok, overlay_ok, same_origin):
    media, store = runtime.live.media, runtime.store
    avatar = LiveAvatar(settings)

    async def require_view(request: Request):
        if not (admin_ok(request) or overlay_ok(request)):
            raise HTTPException(401, "需要管理或播放器凭证")

    def view(*, operator=False):
        queued = []
        for identity in runtime.live.queue:
            job = store.job(identity)
            event = store.event(job["event_id"])["payload"]
            snapshot = json.loads(job["media"]) if job.get("media") else None
            if not operator and snapshot is None:
                continue
            is_online = bool(snapshot and snapshot.get("online"))
            if snapshot:
                snapshot = {
                    "mode": snapshot["mode"],
                    "song": {key: snapshot["song"][key] for key in ("id", "title", "artist")},
                }
            queued.append(
                {
                    "id": identity,
                    "type": event["type"],
                    "text": event["text"] if operator else snapshot["song"]["title"],
                    "requester": event["display_name"],
                    "media": snapshot,
                    "preparation": runtime.live.online_songs.preparation_status(identity)
                    if is_online and operator
                    else None,
                }
            )
        events = store.all(
            "SELECT json_extract(payload,'$.type') AS type, "
            "json_extract(payload,'$.display_name') AS display_name, "
            "json_extract(payload,'$.text') AS text "
            "FROM events WHERE session_id=? AND status NOT IN ('filtered','expired','dropped') "
            "AND json_extract(payload,'$.type') IN ('chat','gift') ORDER BY id DESC LIMIT 20",
            (runtime.live.session_id,),
        )
        character = runtime.character
        return {
            "scene": media.scene(),
            "queue": queued,
            "events": list(reversed(events)),
            "character": {key: character[key] for key in ("id", "name", "version")},
            "assets": settings.assets(character),
            "live2d": avatar.projection(character["id"]),
            "playback": runtime.live.playback,
            "state": runtime.live.state,
            "muted": runtime.live.muted,
            "server_time": time.time(),
            "overlay_connected": runtime.live.overlay is not None,
            "activities": list(runtime.live.recent_activities),
            "poll": (runtime.live.activity_filter.active_poll or {}).get("public"),
            "weather_attribution": bool(runtime.live.options().weather and not settings.data.get("commercial_mode")),
        }

    @app.get("/live")
    async def workbench():
        return FileResponse(Path(__file__).parents[1] / "web/live.html")

    @app.get("/api/live/view", dependencies=[Depends(require_view)])
    async def live_view(request: Request):
        return view(operator=admin_ok(request))

    @app.get("/api/live/studio", dependencies=[Depends(require_admin)])
    async def live_studio():
        inference = runtime.inference.status or {}
        pending = (inference.get("operation") or {}).get("mode")
        return view(operator=True) | room_feed(runtime.live) | {
            "live_enabled": settings.data.get("features", {}).get("live_enabled", False),
            "inference_transitioning": pending is not None and pending != inference.get("mode"),
        }

    @app.get("/media/live2d/{group}/{relative:path}", dependencies=[Depends(require_view)])
    async def live2d_file(group: str, relative: str):
        try:
            path = avatar.file(runtime.character["id"], group, relative)
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(404, "Live2D 文件未安装") from None
        return FileResponse(path, headers={"Cache-Control": "private, max-age=60"})

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
        try:
            async with (
                asyncio.timeout(90),
                runtime.cache.write(
                    "music", int(request.headers.get("content-length") or MAX_UPLOAD)
                ) as allowed,
            ):
                return await receive_upload(request, allowed)
        except TimeoutError:
            raise ValueError("歌曲上传超过90秒，已取消并释放临时文件") from None

    async def receive_upload(request, allowed):
        descriptor, name = tempfile.mkstemp(prefix=".upload-", dir=media.directory)
        temporary = Path(name)
        size, digest = 0, hashlib.sha256()
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > min(MAX_UPLOAD, allowed):
                        raise HTTPException(413, "音频上限96MiB")
                    digest.update(chunk)
                    output.write(chunk)
            info = await asyncio.to_thread(inspect_audio, temporary)
            identity = digest.hexdigest()
            target = media.directory / (identity + "." + info["format"])
            if target.is_symlink():
                raise ValueError("音乐文件不能使用链接")
            if not target.exists():
                os.replace(temporary, target)
            runtime.cache.touch(target, seconds=300)
            return {"id": identity, **info, "bytes": size}
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/media/music/{identity}", dependencies=[Depends(require_view)])
    async def music_file(identity: str):
        path = media.blob(identity)
        runtime.cache.touch(path)
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix])

    @app.get("/api/cache", dependencies=[Depends(require_admin)])
    async def cache_status():
        return runtime.cache.status()

    @app.put("/api/cache", dependencies=[Depends(require_admin)])
    async def cache_policy(data: CachePolicy):
        return await runtime.cache.configure(data.model_dump())

    @app.post("/api/cache/{category}/clear", dependencies=[Depends(require_admin)])
    async def clear_cache(category: str):
        return {"result": await runtime.cache.clear(category), **runtime.cache.status()}

    @app.get("/api/live/online", dependencies=[Depends(require_admin)])
    async def online_status():
        return runtime.live.online_songs.status()

    @app.put("/api/live/online", dependencies=[Depends(require_admin)])
    async def configure_online(data: SongRequestSettings):
        return await runtime.live.online_songs.configure(data.model_dump())

    @app.post("/api/live/online/restart", dependencies=[Depends(require_admin)])
    async def restart_online():
        if not runtime.live.online_songs.config()["enabled"]:
            raise ValueError("请先启用在线点歌")
        return await runtime.live.online_songs.configure(runtime.live.online_songs.config(), restart=True)

    @app.get("/api/live/online/search", dependencies=[Depends(require_admin)])
    async def online_search(q: str):
        if not q.strip() or len(q) > 100:
            raise ValueError("请输入100字以内的歌名／歌手")
        return {"songs": await runtime.live.online_songs.search(q)}

    @app.post("/api/live/online/confirm", dependencies=[Depends(require_admin)])
    async def confirm_online(data: ConfirmSong):
        event, snapshot = runtime.live.online_songs.confirm(data.token, data.number)
        return await runtime.submit(event, online_snapshot=snapshot)

    @app.put("/api/live/queue", dependencies=[Depends(require_admin)])
    async def reorder(data: list[str] = Body(...)):
        if len(data) != len(set(data)) or set(data) != set(runtime.live.queue):
            raise ValueError("队列已改变，请刷新后重排完整待播队列")
        runtime.live.queue[:] = data
        runtime.live.online_songs.sync()
        return {"queue": data}

    @app.delete("/api/live/queue/{identity}", dependencies=[Depends(require_admin)])
    async def remove_queued(identity: str):
        if identity not in runtime.live.queue:
            raise ValueError("该任务已开始或不在队列中")
        runtime.live.queue.remove(identity)
        job = store.job(identity)
        store.update_job(identity, status="cancelled", error="人工移除待播任务")
        store.execute(
            "UPDATE events SET status='cancelled',reason=? WHERE id=?", ("人工移除", job["event_id"])
        )
        runtime.live.online_songs.sync()
        return {"removed": identity}

    @app.post("/api/live/mock-event", dependencies=[Depends(require_admin)])
    async def mock_event(event: Event):
        return await runtime.submit(event)

    @app.get("/api/live/options", dependencies=[Depends(require_admin)])
    async def live_options():
        return runtime.live.options()

    @app.put("/api/live/options", dependencies=[Depends(require_admin)])
    async def set_live_options(data: LiveOptions):
        return runtime.live.set_options(data)

    @app.post("/api/live/poll/start", dependencies=[Depends(require_admin)])
    async def start_poll(data: LivePoll):
        return runtime.live.start_poll(data)

    @app.post("/api/live/poll/finish", dependencies=[Depends(require_admin)])
    async def finish_poll():
        return runtime.live.finish_poll()

    @app.post("/api/live/{action}", dependencies=[Depends(require_admin)])
    async def control(action: str):
        return await runtime.control(action)

    @app.get("/api/events", dependencies=[Depends(require_admin)])
    async def events(limit: int = Query(50, ge=1, le=200)):
        rows = store.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    @app.get("/api/jobs", dependencies=[Depends(require_admin)])
    async def jobs(limit: int = Query(50, ge=1, le=200)):
        rows = store.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        for row in rows:
            row.pop("playback_token", None)
        return rows

    @app.get("/api/interactive-rules", dependencies=[Depends(require_admin)])
    async def get_interactive_rules():
        return runtime.live.interactive_rules.get_rules().model_dump()

    @app.put("/api/interactive-rules", dependencies=[Depends(require_admin)])
    async def put_interactive_rules(data: dict = Body(...)):
        return runtime.live.interactive_rules.save_rules(data)

    @app.get("/api/games/status", dependencies=[Depends(require_admin)])
    async def get_games_status():
        adapter = runtime.live.game_livestream.get_active_adapter()
        return {
            "active_game_id": runtime.live.game_livestream.active_game_id,
            "is_active": runtime.live.game_livestream.is_active(),
            "active_status": adapter.get_status(),
            "available_games": [
                {"id": k, "display_name": a.display_name}
                for k, a in runtime.live.game_livestream.adapters.items()
            ],
        }

    @app.post("/api/games/switch", dependencies=[Depends(require_admin)])
    async def switch_game(data: dict = Body(...)):
        target = data.get("game_id")
        if not target:
            raise ValueError("缺少 game_id")
        return runtime.live.game_livestream.switch_game(target, reason="主播后台手动切换")

    @app.post("/api/games/action", dependencies=[Depends(require_admin)])
    async def execute_game_action(data: dict = Body(...)):
        action = data.get("action")
        if not action:
            raise ValueError("缺少 action")
        adapter = runtime.live.game_livestream.get_active_adapter()
        return adapter.execute_action(action, data.get("params"))

    @app.post("/api/games/step", dependencies=[Depends(require_admin)])
    async def step_game():
        return runtime.live.game_livestream.step_game()

    @app.get("/api/draw-and-guess/status", dependencies=[Depends(require_admin)])
    async def get_draw_and_guess_status():
        return runtime.live.draw_and_guess.get_overlay_state()

    @app.post("/api/draw-and-guess/start", dependencies=[Depends(require_admin)])
    async def start_draw_and_guess(data: dict = Body(...)):
        user = data.get("trigger_user", "主播")
        duration = int(data.get("duration_sec", 60))
        return runtime.live.draw_and_guess.start_round(trigger_user=user, duration_sec=duration)

    @app.post("/api/draw-and-guess/stop", dependencies=[Depends(require_admin)])
    async def stop_draw_and_guess():
        return runtime.live.draw_and_guess.stop_round()

    @app.get("/api/draw-and-guess/overlay")
    async def draw_and_guess_overlay_data():
        return runtime.live.draw_and_guess.get_overlay_state()

    @app.websocket("/ws/overlay")
    async def overlay_ws(websocket: WebSocket):
        if not same_origin(websocket) or not overlay_ok(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        owner = await runtime.live.attach_overlay(websocket)
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 4096:
                    await websocket.close(code=1009)
                    break
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        await runtime.live.acknowledge(owner, data)
                except json.JSONDecodeError:
                    pass
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await runtime.live.detach_overlay(owner)
