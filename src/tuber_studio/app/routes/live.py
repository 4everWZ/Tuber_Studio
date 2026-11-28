"""L4 live routes (`/api/v1/live/*`), live media endpoints, and overlay WebSocket (`/ws/overlay`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from uuid import UUID

from fastapi import Body, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import Field

from ...core.models import Event, StrictModel
from ...live.activity_filter import LivePoll
from ...live.avatar import LiveAvatar
from ...live.games.stardew_device import DeviceCommand, create_stardew_device
from ...live.media import MAX_UPLOAD, MEDIA_TYPES, MusicSettings, default_scene, inspect_audio
from ...live.music.online_songs import SongRequestSettings
from ...live.options import LiveOptions
from ...live.session.schedules import LiveScheduleCreate, LiveScheduleUpdate
from ...live.studio import room_feed
from ...persona.characters.performance import performance
from .system import ERROR_RESPONSES


class ConfirmSong(StrictModel):
    token: str = Field(min_length=20, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    number: int = Field(ge=1, le=20, strict=True)


def register_live_routes(app, settings, runtime, auth, *, on_stardew_device=None) -> None:
    """Register all `/api/v1/live/*` routes, live media routes, and `WS /ws/overlay`."""
    device = create_stardew_device()
    app.state.stardew_device = device
    if device.directory and on_stardew_device is not None:
        on_stardew_device(device)
    if getattr(runtime, "live_facade", None) is not None:
        runtime.live_facade.attach_stardew_device(device)

    media, store = runtime.live.media, runtime.store
    avatar = LiveAvatar(settings)
    admin_dep = [Depends(auth.require_admin)]
    view_dep = [Depends(auth.require_view)]
    prefix = "/api/v1/live"

    def view(*, operator: bool = False):
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
        show = performance(store)
        return {
            "scene": media.scene(),
            "queue": queued,
            "events": list(reversed(events)),
            "character": {
                "id": character["id"],
                "version": character["version"],
                "name": store.display_name(character["id"]),
            },
            "performance": show,
            "lip_sync": {
                "provider": "ditto",
                "status": "tbd" if show["avatar_enabled"] else "off",
                "renderer": "pet" if show["pets_enabled"] else "none",
            },
            "assets": settings.assets(character) if show["pets_enabled"] else [],
            "live2d": None,
            "playback": runtime.live.playback,
            "state": runtime.live.state,
            "muted": runtime.live.muted,
            "server_time": time.time(),
            "overlay_connected": runtime.live.overlay is not None,
            "activities": list(runtime.live.recent_activities),
            "poll": (runtime.live.activity_filter.active_poll or {}).get("public"),
            "weather_attribution": bool(runtime.live.options().weather and not settings.data.get("commercial_mode")),
        }

    async def receive_upload(request: Request, allowed: int):
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

    @app.get(prefix + "/view", dependencies=view_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def live_view(request: Request):
        return view(operator=auth.admin_ok(request))

    @app.get(prefix + "/studio", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def live_studio():
        return (
            view(operator=True)
            | room_feed(runtime.live)
            | {
                "live_enabled": settings.data.get("features", {}).get("live_enabled", False),
                "inference_transitioning": False,
            }
        )

    @app.get("/media/live2d/{group}/{relative:path}", dependencies=view_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def live2d_file(group: str, relative: str):
        try:
            path = avatar.file(runtime.character["id"], group, relative)
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(404, "Live2D 文件未安装") from None
        return FileResponse(path, headers={"Cache-Control": "private, max-age=60"})

    @app.get("/media/music/{identity}", dependencies=view_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def music_file(identity: str):
        path = media.blob(identity)
        runtime.cache.touch(path)
        return FileResponse(path, media_type=MEDIA_TYPES[path.suffix])

    @app.put(prefix + "/scene", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def save_scene(data: dict = Body(...)):
        return media.save_scene(data)

    @app.get(prefix + "/scene/default", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def reset_scene():
        return default_scene()

    @app.get(prefix + "/music", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def music():
        return {
            "settings": media.config(),
            "songs": media.songs(),
            "singing": media.singing.describe(runtime.character),
        }

    @app.put(prefix + "/music", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def save_music(data: MusicSettings):
        return media.save_music_settings(data, runtime.character)

    @app.put(prefix + "/songs/{identity}", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def save_song(identity: str, data: dict = Body(...)):
        if data.get("id") != identity:
            raise ValueError("歌曲ID不一致")
        return await media.save_song(data)

    @app.delete(prefix + "/songs/{identity}", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def remove_song(identity: str):
        store.set("music_catalog", [song for song in media.songs() if song["id"] != identity])
        return {"removed": identity}

    @app.post(prefix + "/uploads", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def upload(request: Request):
        try:
            async with (
                asyncio.timeout(90),
                runtime.cache.write("music", int(request.headers.get("content-length") or MAX_UPLOAD)) as allowed,
            ):
                return await receive_upload(request, allowed)
        except TimeoutError:
            raise ValueError("歌曲上传超过90秒，已取消并释放临时文件") from None

    @app.get(prefix + "/online", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def online_status():
        return runtime.live.online_songs.status()

    @app.put(prefix + "/online", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def configure_online(data: SongRequestSettings):
        return await runtime.live.online_songs.configure(data.model_dump())

    @app.post(prefix + "/online/restart", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def restart_online():
        if not runtime.live.online_songs.config()["enabled"]:
            raise ValueError("请先启用在线点歌")
        return await runtime.live.online_songs.configure(runtime.live.online_songs.config(), restart=True)

    @app.get(prefix + "/online/search", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def online_search(q: str):
        if not q.strip() or len(q) > 100:
            raise ValueError("请输入100字以内的歌名／歌手")
        return {"songs": await runtime.live.online_songs.search(q)}

    @app.post(prefix + "/online/confirm", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def confirm_online(data: ConfirmSong):
        event, snapshot = runtime.live.online_songs.confirm(data.token, data.number)
        return await runtime.submit(event, online_snapshot=snapshot)

    @app.put(prefix + "/queue", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def reorder(data: list[str] = Body(...)):
        if len(data) != len(set(data)) or set(data) != set(runtime.live.queue):
            raise ValueError("队列已改变，请刷新后重排完整待播队列")
        runtime.live.queue[:] = data
        runtime.live.online_songs.sync()
        return {"queue": data}

    @app.delete(prefix + "/queue/{identity}", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def remove_queued(identity: str):
        if identity not in runtime.live.queue:
            raise ValueError("该任务已开始或不在队列中")
        runtime.live.queue.remove(identity)
        job = store.job(identity)
        store.update_job(identity, status="cancelled", error="人工移除待播任务")
        store.execute("UPDATE events SET status='cancelled',reason=? WHERE id=?", ("人工移除", job["event_id"]))
        runtime.live.online_songs.sync()
        return {"removed": identity}

    @app.post(prefix + "/mock-event", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def mock_event(event: Event):
        return await runtime.submit(event)

    @app.get(prefix + "/options", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def live_options():
        return runtime.live.options()

    @app.put(prefix + "/options", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def set_live_options(data: LiveOptions):
        return runtime.live.set_options(data)

    @app.post(prefix + "/poll/start", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def start_poll(data: LivePoll):
        return runtime.live.start_poll(data)

    @app.post(prefix + "/poll/finish", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def finish_poll():
        return runtime.live.finish_poll()

    def cold_director_snapshot() -> dict:
        raw = runtime.live.cold_director.status()
        return raw | {
            "idle_seconds": raw.get("elapsed_seconds", 0.0),
            "next_action_in_seconds": raw.get("seconds_until_next"),
            "cadence_seconds": getattr(runtime.live.cold_director, "tier3_cadence", 300.0),
            "active": runtime.live.state == "live",
        }

    @app.get(prefix + "/schedules", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def list_live_schedules():
        return runtime.live.live_schedules.status()

    @app.post(prefix + "/schedules", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def create_live_schedule(body: LiveScheduleCreate):
        schedule = await runtime.live.live_schedules.create(body)
        return {
            "schedule": schedule,
            "status": runtime.live.live_schedules.status(),
        }

    @app.patch(prefix + "/schedules/{schedule_id}", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def update_live_schedule(schedule_id: str, body: LiveScheduleUpdate):
        schedule = await runtime.live.live_schedules.update(schedule_id, body)
        return {
            "schedule": schedule,
            "status": runtime.live.live_schedules.status(),
        }

    @app.delete(prefix + "/schedules/{schedule_id}", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def delete_live_schedule(schedule_id: str):
        deleted = await runtime.live.live_schedules.delete(schedule_id)
        return {
            "deleted": deleted["id"],
            "status": runtime.live.live_schedules.status(),
        }

    @app.get(prefix + "/overtime", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def get_overtime_status():
        return runtime.live.overtime_theater.status()

    @app.post(prefix + "/overtime/start", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def start_overtime_window(data: dict = Body(default_factory=dict)):
        return runtime.live.overtime_theater.start_window(
            base_duration_minutes=int(data.get("base_duration_minutes", 60)),
            max_overtime_minutes=int(data.get("max_overtime_minutes", 120)),
            extension_step_minutes=int(data.get("extension_step_minutes", 15)),
        )

    @app.post(prefix + "/overtime/stop", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def stop_overtime_window():
        return runtime.live.overtime_theater.stop()

    @app.get(prefix + "/shows/status", responses=ERROR_RESPONSES, tags=["live"])
    async def get_live_shows_status():
        return {
            "character": store.display_name(runtime.character["id"]),
            "draw_and_guess": runtime.live.draw_and_guess.get_overlay_state(),
            "turtle_soup": runtime.live.turtle_soup.get_overlay_state(),
            "roast_radio": runtime.live.roast_radio.get_overlay_state(),
            "cold_director": cold_director_snapshot(),
            "overtime": runtime.live.overtime_theater.status(),
        }

    @app.post(prefix + "/shows/turtle-soup/start", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def start_turtle_soup(data: dict = Body(default_factory=dict)):
        trigger_user = str(data.get("trigger_user") or "主播")
        genre = data.get("genre")
        use_llm = bool(data.get("use_llm", True))
        if use_llm:
            return await runtime.live.turtle_soup.start_round_llm(
                runtime.llm, trigger_user=trigger_user, genre=genre
            )
        return runtime.live.turtle_soup.start_round(trigger_user=trigger_user, genre=genre)

    @app.post(prefix + "/shows/turtle-soup/hint", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def hint_turtle_soup():
        return runtime.live.turtle_soup.give_hint()

    @app.post(prefix + "/shows/turtle-soup/reveal", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def reveal_turtle_soup(data: dict = Body(default_factory=dict)):
        return runtime.live.turtle_soup.reveal_and_stop(winner=data.get("winner"))

    @app.post(prefix + "/shows/roast-radio/start-debate", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def start_roast_radio_debate(data: dict = Body(default_factory=dict)):
        topic = str(data.get("topic") or "今晚宵夜吃白饭还是泡面")
        return runtime.live.roast_radio.start_debate(
            topic=topic,
            side_a=str(data.get("side_a") or "正方"),
            side_b=str(data.get("side_b") or "反方"),
            trigger_user=str(data.get("trigger_user") or "主播"),
        )

    @app.post(prefix + "/shows/roast-radio/roast", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def trigger_roast_radio(data: dict = Body(default_factory=dict)):
        return await runtime.live.roast_radio.generate_roast_segment(
            runtime.llm, store, topic=data.get("topic")
        )

    @app.post(prefix + "/shows/roast-radio/stop", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def stop_roast_radio():
        return runtime.live.roast_radio.stop()

    @app.get(prefix + "/cold-director/status", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def get_cold_director_status():
        return cold_director_snapshot()

    @app.post(prefix + "/cold-director/trigger", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def trigger_cold_director(data: dict = Body(default_factory=dict)):
        director = runtime.live.cold_director
        tier_override = data.get("tier")
        now_ts = time.monotonic()
        if tier_override is not None:
            tier_val = int(tier_override)
            tier_offsets = {1: 95.0, 2: 305.0, 3: 905.0}
            offset = tier_offsets.get(tier_val, 95.0)
            director.last_viewer_activity = now_ts - offset
            director.last_action_at = None
            target_now = now_ts
        else:
            if director.current_tier(now_ts) == 0:
                director.last_viewer_activity = now_ts - 95.0
            due = director.next_action_due(now_ts)
            target_now = max(now_ts, due) if due is not None else now_ts
        segment = await director.next_segment(
            now=target_now,
            llm=runtime.llm,
            draw_and_guess=runtime.live.draw_and_guess,
            turtle_soup=runtime.live.turtle_soup,
            roast_radio=runtime.live.roast_radio,
            store=store,
            options=runtime.live.options(),
        )
        return {
            "spoken_text": (segment or {}).get("spoken_text"),
            "segment": segment,
            "status": cold_director_snapshot(),
        }

    @app.post(prefix + "/{action}", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def control(action: str):
        return await runtime.control(action)

    @app.get(prefix + "/events", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def events(limit: int = Query(50, ge=1, le=200)):
        rows = store.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows

    @app.get(prefix + "/jobs", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def jobs(limit: int = Query(50, ge=1, le=200)):
        rows = store.all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        for row in rows:
            row.pop("playback_token", None)
        return rows

    @app.get(prefix + "/interactive-rules", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def get_interactive_rules():
        return runtime.live.interactive_rules.get_rules().model_dump()

    @app.put(prefix + "/interactive-rules", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def put_interactive_rules(data: dict = Body(...)):
        return runtime.live.interactive_rules.save_rules(data)

    @app.get(prefix + "/games/status", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def get_games_status():
        return runtime.live.game_livestream.status()

    @app.post(prefix + "/games/control", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def control_game(data: dict = Body(...)):
        return runtime.live.game_livestream.transfer_control(data.get("owner"))

    @app.post(prefix + "/games/switch", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def switch_game(data: dict = Body(...)):
        target = data.get("game_id")
        if not target:
            raise ValueError("缺少 game_id")
        return runtime.live.game_livestream.switch_game(target, reason="主播后台手动切换")

    @app.post(prefix + "/games/action", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def execute_game_action(data: dict = Body(...)):
        action = data.get("action")
        if not action:
            raise ValueError("缺少 action")
        return runtime.live.game_livestream.manual_action(action, data.get("params"))

    @app.post(prefix + "/games/step", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def step_game():
        return runtime.live.game_livestream.step_game()

    @app.get(prefix + "/draw-and-guess/status", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def get_draw_and_guess_status():
        return runtime.live.draw_and_guess.get_overlay_state()

    @app.post(prefix + "/draw-and-guess/start", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def start_draw_and_guess(data: dict = Body(default_factory=dict)):
        user = str(data.get("trigger_user") or "主播")
        duration = int(data.get("duration_sec", 60))
        theme = data.get("theme")
        mode = str(data.get("mode") or "aqua_draws")
        use_llm = bool(data.get("use_llm", False))
        if use_llm:
            return await runtime.live.draw_and_guess.start_round_llm(
                runtime.llm,
                trigger_user=user,
                duration_sec=duration,
                theme=theme,
                mode=mode,
            )
        return runtime.live.draw_and_guess.start_round(
            trigger_user=user,
            duration_sec=duration,
            theme=theme,
            mode=mode,
        )

    @app.post(prefix + "/draw-and-guess/stop", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    async def stop_draw_and_guess():
        return runtime.live.draw_and_guess.stop_round()

    @app.get(prefix + "/draw-and-guess/overlay", responses=ERROR_RESPONSES, tags=["live"])
    async def draw_and_guess_overlay_data():
        return runtime.live.draw_and_guess.get_overlay_state() | {
            "character": store.display_name(runtime.character["id"])
        }

    @app.get(prefix + "/games/device/stardew", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    def stardew_state():
        return device.status()

    @app.post(prefix + "/games/device/stardew/actions", dependencies=admin_dep, responses=ERROR_RESPONSES, tags=["live"])
    def stardew_submit(command: DeviceCommand):
        try:
            return device.submit(command)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get(
        prefix + "/games/device/stardew/actions/{session}/{identity}",
        dependencies=admin_dep,
        responses=ERROR_RESPONSES,
        tags=["live"],
    )
    def stardew_receipt(session: UUID, identity: UUID):
        result = device.receipt(session, identity)
        if result is None:
            raise HTTPException(404, "没有这个实机会话的动作记录")
        return result

    @app.websocket("/ws/overlay")
    async def overlay_ws(websocket: WebSocket):
        if not auth.same_origin(websocket) or not auth.overlay_ok(websocket):
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
