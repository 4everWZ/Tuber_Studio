"""Tests for non-VIP comment GIF gating, runtime 49044 fallback, and disappeared target edge cases."""

import io
import json
import time

import httpx
import pytest
from PIL import Image

from tests.support.operations_helpers import BV, authorize, finished, wait_for_sends
from tests.support.operations_helpers import browsing as browsing
from tuber_studio.core.errors import PlatformRejected, platform_rejection
from tuber_studio.infra.platforms.bilibili.account import (
    PROFILE_CACHE_KEY,
    BilibiliAccount,
    account_comment_capabilities,
    record_comment_capability_restriction,
)
from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter, PlatformReadError
from tuber_studio.infra.platforms.bilibili.client import BilibiliAdapter
from tuber_studio.operations.channels.attachments import images as ops
from tuber_studio.operations.channels.attachments.attachments import TEXT_ONLY_IMAGE
from tuber_studio.operations.discovery.batches import run_discovery_video
from tuber_studio.persona.assets.memes import MemeCatalog


def _animated_webp_bytes():
    frames = [Image.new("RGB", (64, 64), "red"), Image.new("RGB", (64, 64), "blue")]
    buf = io.BytesIO()
    frames[0].save(buf, format="WEBP", save_all=True, append_images=[frames[1]], duration=100, loop=0)
    return buf.getvalue()


async def test_bilibili_client_and_account_track_vip_and_comment_capabilities(settings, store):
    vip_flag = {"status": 0, "type": 1, "level": 2}

    def handler(request):
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "isLogin": True,
                        "mid": 100,
                        "uname": "Aqua",
                        "face": "https://i0.hdslb.com/bfs/face/avatar.jpg",
                        "money": 12.0,
                        "vipStatus": vip_flag["status"],
                        "vipType": vip_flag["type"],
                        "vip": {"status": vip_flag["status"], "type": vip_flag["type"]},
                        "level_info": {"current_level": vip_flag["level"]},
                    },
                },
            )
        if request.url.path == "/x/space/myinfo":
            return httpx.Response(200, json={"code": 0, "data": {"mid": 100, "name": "Aqua", "sign": "hi"}})
        if request.url.path == "/x/relation/stat":
            return httpx.Response(200, json={"code": 0, "data": {"follower": 9}})
        raise AssertionError(request.url.path)

    client = BilibiliAdapter({"sessdata": "sess", "csrf": "csrf"})
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    try:
        profile = await client.account_profile()
        assert profile["vip_active"] is False
        assert profile["vip_type"] == 1
        assert profile["level"] == 2
    finally:
        await client.close()

    account = BilibiliAccount(settings, store, adapter=BrowsingAdapter({"sessdata": "sess", "csrf": "csrf"}))
    await account.adapter.close()
    account.adapter = BrowsingAdapter({"sessdata": "sess", "csrf": "csrf"})
    account.adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com",
        transport=httpx.MockTransport(handler),
    )
    account.credentials = {"sessdata": "sess", "csrf": "csrf"}
    try:
        status = await account.refresh()
        assert status["state"] == "logged_in"
        caps = status["comment_capabilities"]
        assert caps["vip_active"] is False
        assert caps["comment_allowed"] is True
        assert caps["comment_image_allowed"] is True
        assert caps["comment_gif_allowed"] is False
        assert "大会员" in caps["reason"]

        record_comment_capability_restriction(store, "100", gif_allowed=False, reason="code 49044")
        vip_flag.update(status=1, type=2)
        renewed = await account.refresh()
        assert renewed["comment_capabilities"]["vip_active"] is True
        assert renewed["comment_capabilities"]["comment_gif_allowed"] is True

        vip_flag["level"] = 0
        lv0 = await account.refresh()
        assert lv0["comment_capabilities"]["comment_allowed"] is False
        assert lv0["comment_capabilities"]["comment_gif_allowed"] is False
    finally:
        await account.close()


async def test_operation_images_excludes_gifs_for_non_vip_comments_only(settings, store, monkeypatch):
    catalog = MemeCatalog(settings, store)
    gif_data = (settings.root / "assets/whale/cheerful.gif").read_bytes()
    png_data = (settings.root / "assets/whale/memes/twirl.png").read_bytes()
    webp_data = _animated_webp_bytes()
    orig_inspect = catalog.inspector.inspect

    async def fake_inspect(content, *, mime=None):
        if content[:6] in (b"GIF87a", b"GIF89a"):
            return await orig_inspect(content, mime=mime)
        if content.startswith(b"\x89PNG"):
            return {"extension": "png", "mime_type": "image/png", "width": 384, "height": 384, "frames": 1, "animated": False}
        return {"extension": "webp", "mime_type": "image/webp", "width": 64, "height": 64, "frames": 2, "animated": True}

    monkeypatch.setattr(catalog.inspector, "inspect", fake_inspect)
    common = {
        "emotions": ["cheerful"],
        "source": {"kind": "user_upload", "author": "test"},
        "license": {"name": "fixture"},
    }
    gif_asset = await catalog.register("whale", gif_data, title="动图笑", tags=["开心"], **common)
    png_asset = await catalog.register("whale", png_data, title="静态转圈", tags=["开心"], **common)
    webp_asset = await catalog.register("whale", webp_data, title="动态WebP", tags=["开心"], **common)

    store.set(
        PROFILE_CACHE_KEY,
        {
            "format": 1,
            "credential_signature": "sig",
            "checked_at": time.time(),
            "profile": {"uid": "100", "name": "Aqua", "vip_active": False, "level": 2},
        },
    )
    store.set("illustration_policy", {"use_memes": True, "generate_images": False})

    service = ops.OperationImages(settings, store)
    service.catalog = catalog
    comment_scope = ops.ImageScope(
        account_id="100",
        character_id="whale",
        channel="comment",
        source_id=json.dumps([1, "video", BV, "1", "0", "0", ""], separators=(",", ":")),
    )
    comment_job = service.decide(comment_scope, request_key="k1", context={"bvid": BV}, draft="你好")
    raw_job = store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (comment_job["job_id"],))
    comment_material = service._choice_material(comment_scope, raw_job)
    offered_comment_ids = {item["asset_id"] for item in comment_material["gallery"]}
    assert offered_comment_ids == {png_asset["asset_id"]}
    assert gif_asset["asset_id"] not in offered_comment_ids
    assert webp_asset["asset_id"] not in offered_comment_ids

    dm_scope = ops.ImageScope(account_id="100", character_id="whale", channel="direct_message", source_id="dm-1")
    dm_job = service.decide(dm_scope, request_key="k2", context={"incoming": "hi"}, draft="嗨")
    raw_dm = store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (dm_job["job_id"],))
    dm_material = service._choice_material(dm_scope, raw_dm)
    offered_dm_ids = {item["asset_id"] for item in dm_material["gallery"]}
    assert {gif_asset["asset_id"], png_asset["asset_id"], webp_asset["asset_id"]} <= offered_dm_ids


@pytest.mark.parametrize(
    ("code", "expected_kind"),
    [
        (62002, "not_found"),
        (62004, "not_found"),
        (62012, "not_found"),
        (12022, "not_found"),
        (-404, "not_found"),
        (12002, "closed"),
        (12061, "closed"),
    ],
)
async def test_bilibili_read_error_taxonomy_and_discovery_no_retry_wait(browsing, code, expected_kind):
    def handler(request):
        return httpx.Response(200, json={"code": code, "message": f"err {code}"})

    adapter = BrowsingAdapter({"sessdata": "s", "csrf": "c"})
    adapter.client = httpx.AsyncClient(base_url="https://api.bilibili.com", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(PlatformReadError) as exc_info:
            await adapter._get("/x/web-interface/view", {"bvid": BV})
        assert exc_info.value.kind == expected_kind
        assert exc_info.value.code == code
    finally:
        await adapter.close()

    await finished(browsing, preview=True)
    await authorize(browsing)
    # Disappeared/closed video during discovery finishes as `skipped` cleanly with zero `retry_wait`.
    async def missing_video(bvid):
        raise PlatformReadError(expected_kind, f"code {code}", code=code)

    browsing.adapter.video_metadata = missing_video
    row = browsing.repo.round(browsing.intents()[0]["round_id"])
    browsing.store.execute(
        "UPDATE browsing_rounds SET status='running',preview=0,epoch=? WHERE id=?",
        (browsing.operations.policy()["epoch"], row["id"]),
    )
    metrics = {"failures": 0, "prepared": 0}
    candidate = {"bvid": "BV1missing999", "aid": 999999, "title": "gone", "owner_uid": "200", "owner": "up", "duration": 10}
    await run_discovery_video(browsing, browsing.repo.round(row["id"]), candidate, metrics)
    assert metrics["failures"] == 0
    assert not browsing.store.all("SELECT * FROM browsing_video_retries WHERE bvid='BV1missing999'")
    task = browsing.store.one("SELECT status FROM browsing_tasks WHERE bvid='BV1missing999'")
    assert task["status"] == "skipped"


async def _register_gif_meme(browsing):
    catalog = MemeCatalog(browsing.settings, browsing.store)
    browsing.comments.memes = catalog
    browsing.runtime.operation_images.catalog = catalog
    gif_data = (browsing.settings.root / "assets/whale/cheerful.gif").read_bytes()
    return await catalog.register(
        "whale",
        gif_data,
        title="开心笑",
        emotions=["cheerful"],
        source={"kind": "user_upload", "author": "test"},
        license={"name": "fixture"},
    )


async def test_non_vip_automatic_gif_intent_degrades_before_upload_without_49044(browsing, monkeypatch):
    gif_meme = await _register_gif_meme(browsing)
    await finished(browsing, preview=True)
    main = next(i for i in browsing.intents() if i["kind"] == "top_level")
    target = main["target"] | {"image_ref": {"asset_id": gif_meme["asset_id"], "sha256": gif_meme["sha256"]}}
    await authorize(browsing)
    browsing.store.execute("UPDATE browsing_rounds SET preview=0 WHERE id=?", (main["round_id"],))
    browsing.store.execute(
        "UPDATE browsing_intents SET status='pending_review',auto_authorized=1,target=? WHERE id=?",
        (json.dumps(target, ensure_ascii=False), main["id"]),
    )
    browsing.store.set(
        PROFILE_CACHE_KEY,
        {
            "format": 1,
            "credential_signature": "sig",
            "checked_at": time.time(),
            "profile": {"uid": "100", "name": "Aqua", "vip_active": False, "level": 2},
        },
    )
    browsing.store.execute(
        "UPDATE browsing_intents SET epoch=? WHERE id=?",
        (browsing.operations.policy()["epoch"], main["id"]),
    )
    seen_images = []
    orig_publish = browsing.adapter.publish_top_level

    async def capture_publish(tgt, text, before_send=None, **kwargs):
        seen_images.append(kwargs.get("image"))
        return await orig_publish(tgt, text, before_send=before_send, **kwargs)

    browsing.adapter.publish_top_level = capture_publish
    browsing.queue_publication(main["id"])
    await wait_for_sends(browsing)

    updated = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (main["id"],)))
    assert updated["status"] == "published"
    assert updated["target"]["image_ref"] == TEXT_ONLY_IMAGE
    assert not browsing.store.all("SELECT * FROM operation_image_uploads WHERE draft_id=?", (main["id"],))
    assert seen_images == [None]
    assert len(browsing.adapter.sent) >= 1


async def test_runtime_49044_records_restriction_falls_back_for_auto_and_parks_manual_without_llm(browsing, monkeypatch):
    gif_meme = await _register_gif_meme(browsing)
    await finished(browsing, preview=True)
    main = next(i for i in browsing.intents() if i["kind"] == "top_level")
    target = main["target"] | {"image_ref": {"asset_id": gif_meme["asset_id"], "sha256": gif_meme["sha256"]}}
    browsing.store.execute("UPDATE browsing_rounds SET preview=0 WHERE id=?", (main["round_id"],))
    browsing.store.execute(
        "UPDATE browsing_intents SET status='pending_review',auto_authorized=1,target=? WHERE id=?",
        (json.dumps(target, ensure_ascii=False), main["id"]),
    )

    async def fail_llm(*args, **kwargs):
        pytest.fail("49044 permission rejection must not invoke text-revision LLM")

    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", fail_llm)

    orig_publish = browsing.adapter.publish_top_level
    attempts = []

    async def reject_49044(tgt, text, before_send=None, **kwargs):
        attempts.append(bool(kwargs.get("image")))
        if kwargs.get("image") is not None:
            if before_send:
                before_send()
            raise PlatformRejected(49044, "无评论动图发送权限")
        return await orig_publish(tgt, text, before_send=before_send, **kwargs)

    browsing.adapter.publish_top_level = reject_49044
    await authorize(browsing)
    browsing.store.execute("UPDATE browsing_intents SET epoch=? WHERE id=?", (browsing.operations.policy()["epoch"], main["id"]))

    res = await browsing.publish(main["id"], automatic=True)
    assert res["status"] == "published"
    assert attempts == [True, False]
    assert account_comment_capabilities(browsing.store, "100")["comment_gif_allowed"] is False

    # Manual send hitting 49044 parks cleanly in pending_review and clears send queue without LLM calls.
    manual_id = "manual-49044-intent"
    browsing.store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,epoch,"
        "auto_authorized,status,text,reason,target,evidence,material_keys,input_version,receipt,visibility,error,"
        "created_at,updated_at,source_pipeline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            manual_id,
            main["account_id"],
            main["character_id"],
            main["character_version"],
            main["source_key"] + "-manual",
            "top_level",
            main["round_id"],
            browsing.operations.policy()["epoch"],
            0,
            "pending_review",
            "手动测试发送",
            "r",
            json.dumps(target | {"image_ref": {"asset_id": gif_meme["asset_id"], "sha256": gif_meme["sha256"]}}),
            json.dumps(main["evidence"]),
            json.dumps(main["material_keys"]),
            main["input_version"],
            None,
            "not_checked",
            None,
            time.time(),
            time.time(),
            "discovery",
        ),
    )
    browsing.store.set("bilibili_comment_capabilities:100", {})
    browsing.review(manual_id, "approve")
    with pytest.raises(PlatformRejected) as err:
        await browsing.publish(manual_id, automatic=False)
    assert err.value.code == 49044
    manual_row = browsing.store.one("SELECT status, error FROM browsing_intents WHERE id=?", (manual_id,))
    assert manual_row["status"] == "pending_review"
    assert "静态图" in (manual_row["error"] or "")


async def test_disappeared_video_and_terminal_rejections_settle_cleanly_without_llm_or_send_queue(browsing, monkeypatch):
    await _register_gif_meme(browsing)
    await finished(browsing, preview=True)
    intents = browsing.intents()
    main = next(i for i in intents if i["kind"] == "top_level")
    reply_id = "reply-12002-intent"
    browsing.store.execute("UPDATE browsing_rounds SET preview=0 WHERE id=?", (main["round_id"],))

    async def fail_llm(*args, **kwargs):
        pytest.fail("terminal target unavailability must not invoke text-revision LLM")

    monkeypatch.setattr(browsing.runtime.operation_llm, "generate_json", fail_llm)

    # 1. Pre-send top_level video disappeared (code 62002) clears queue and marks rejected_by_platform.
    orig_meta = browsing.adapter.video_metadata

    async def missing_meta(bvid):
        raise PlatformReadError("not_found", "视频已删除或设为不可见（code 62002）", code=62002)

    browsing.adapter.video_metadata = missing_meta
    await authorize(browsing)
    epoch = browsing.operations.policy()["epoch"]
    browsing.store.execute("UPDATE browsing_intents SET status='pending_review',auto_authorized=1,epoch=? WHERE id=?", (epoch, main["id"]))
    browsing.review(main["id"], "approve")
    from tuber_studio.operations.channels.attachments.attachments import comment_payload
    from tuber_studio.operations.kernel.sends import approved_version

    fresh = browsing._send_draft({"channel": "browsing", "source_id": main["id"]})
    browsing.sends.enqueue(
        "browsing",
        main["id"],
        approval=approved_version(fresh, comment_payload(fresh, browsing.runtime, "browsing")),
        scope=(fresh["account_id"], fresh["character_id"], fresh["character_version"]),
    )
    await wait_for_sends(browsing)
    settled_main = browsing.store.one("SELECT status, auto_authorized, error FROM browsing_intents WHERE id=?", (main["id"],))
    assert settled_main["status"] == "rejected_by_platform"
    assert settled_main["auto_authorized"] == 0
    assert "62002" in (settled_main["error"] or "")
    assert not browsing.store.one("SELECT 1 FROM operation_send_queue WHERE source_id=?", (main["id"],))

    # 2. Terminal publish rejection (code 12002 comment closed) removes from queue and marks rejected_by_platform.
    browsing.adapter.video_metadata = orig_meta
    reply_target = main["target"] | {
        "root": "11",
        "parent": "11",
        "recipient": "300",
        "recipient_name": "观众",
        "source_text": "楼层原话",
    }
    browsing.store.execute(
        "INSERT INTO browsing_intents(id,account_id,character_id,character_version,source_key,kind,round_id,epoch,"
        "auto_authorized,status,text,reason,target,evidence,material_keys,input_version,receipt,visibility,error,"
        "created_at,updated_at,source_pipeline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            reply_id,
            main["account_id"],
            main["character_id"],
            main["character_version"],
            main["source_key"] + "-reply12002",
            "reply_public",
            main["round_id"],
            epoch,
            1,
            "pending_review",
            "确实很有意思",
            "r",
            json.dumps(reply_target, ensure_ascii=False),
            json.dumps(main["evidence"]),
            json.dumps(main["material_keys"]),
            main["input_version"],
            None,
            "not_checked",
            None,
            time.time(),
            time.time(),
            "discovery",
        ),
    )

    async def verify_reply_ok(target, account):
        return {"source": {"author_uid": "300", "author_name": "观众", "text": "楼层原话"}, "items": []}

    async def reject_closed(tgt, text, before_send=None):
        if before_send:
            before_send()
        raise platform_rejection({"code": 12002, "message": "评论区已关闭"})

    monkeypatch.setattr(browsing.publication, "_verify_reply_source", verify_reply_ok)
    monkeypatch.setattr(browsing.adapter, "publish_resource_reply", reject_closed)
    browsing.queue_publication(reply_id)
    await wait_for_sends(browsing)
    settled_reply = browsing.store.one("SELECT status, error FROM browsing_intents WHERE id=?", (reply_id,))
    assert settled_reply["status"] == "rejected_by_platform"
    assert "12002" in (settled_reply["error"] or "")
    assert not browsing.store.one("SELECT 1 FROM operation_send_queue WHERE source_id=?", (reply_id,))


async def test_cached_comment_visit_disappeared_video_skips_without_retry_wait(browsing, monkeypatch):
    await authorize(browsing)
    row, _ = browsing.repo.create_round(
        browsing.account,
        browsing.runtime.character,
        browsing.effective() | {"discovery_enabled": True, "reply_enabled": True, "autonomous_batch": True},
        browsing.operations.policy()["epoch"],
        preview=False,
    )
    material = {
        "metadata": {"bvid": BV, "aid": 1, "owner": "UP", "owner_uid": "99", "title": "t", "duration": 60.0},
        "parts": [{"cid": "11", "segments": [{"id": "s1", "start": 0.0, "end": 5.0, "text": "字幕"}]}],
        "coverage": "all_available_cc",
        "source_version": "sv-1",
    }
    browsing.repo.save_content_progress(row, material, [])

    async def missing_comments(*args, **kwargs):
        raise PlatformReadError("not_found", "视频已删除或设为不可见（code 62002）", code=62002)

    monkeypatch.setattr(browsing.adapter, "fetch", missing_comments)
    candidate = {"bvid": BV, "task_kind": "comment_visit"}
    metrics = {"prepared": 0, "failures": 0, "viewed": 0, "skipped": 0, "selected": 0, "drafts": 0}
    await run_discovery_video(browsing, row, candidate, metrics)

    task_row = browsing.store.one("SELECT status, error, payload FROM browsing_tasks WHERE round_id=? AND bvid=?", (row["id"], BV))
    assert task_row["status"] == "skipped"
    assert json.loads(task_row["payload"]).get("reason") == "not_found"
    assert "62002" in (task_row["error"] or "")
    assert browsing.repo.video_retry(row, BV) is None
    assert metrics["failures"] == 0


async def test_discovery_runner_ignores_stop_transition_without_setting_error(browsing, monkeypatch):
    await authorize(browsing)
    browsing.operations.configure({"enabled": True}, account_id=browsing.account, character=browsing.runtime.character)
    await browsing.configure({"discovery_enabled": True})

    async def revisit_and_stop():
        browsing.operations.configure({"enabled": False})
        return 0

    async def should_not_start_run(**kwargs):
        raise AssertionError("browsing.run must not be called once accepting_work is False")

    monkeypatch.setattr(browsing, "run_discovery_revisits_once", revisit_and_stop)
    monkeypatch.setattr(browsing, "run", should_not_start_run)

    await browsing.discovery_runner.run_once()
    assert browsing.discovery.error is None
    assert browsing.discovery.attention is None
