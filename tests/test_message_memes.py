"""Real catalog/worker/ledger with controlled HTTP; no recipient is contacted."""

import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from tuber_studio.bilibili import PublishUncertain
from tuber_studio.bilibili_messages import BilibiliMessagesAdapter
from tuber_studio.comments import Comments
from tuber_studio.direct_messages import DirectMessages
from tuber_studio.memes import MemeCatalog


@pytest.fixture
async def image_http(settings):
    content = (settings.root / "assets/whale/cheerful.gif").read_bytes()
    state = {
        "requests": [],
        "upload": "ok",
        "final": "ok",
        "after_upload": None,
        "content": content,
        "receipt_patch": {},
        "upload_started": asyncio.Event(),
        "nav_entered": asyncio.Event(),
        "nav_wait": None,
    }

    async def handle(request):
        state["requests"].append(request)
        if request.url.path == "/x/web-interface/nav":
            state["nav_entered"].set()
            if state["nav_wait"]:
                await state["nav_wait"].wait()
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "mid": 900,
                        "wbi_img": {
                            "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                            "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                        },
                    },
                },
            )
        if request.url.path == "/x/dynamic/feed/draw/upload_bfs":
            state["upload_started"].set()
            if state["upload"] == "block":
                await asyncio.Event().wait()
            if state["upload"] == "timeout":
                raise httpx.ReadTimeout("controlled preparation timeout")
            if state["upload"] == "failed":
                return httpx.Response(503, json={"code": -1})
            if state["after_upload"]:
                state["after_upload"]()
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "image_url": "https://i0.hdslb.com/bfs/new_dyn/known.gif",
                        "image_width": 384,
                        "image_height": 384,
                        "img_size": len(content) / 1024,
                    }
                    | state["receipt_patch"],
                },
            )
        assert request.url.host == "api.vc.bilibili.com" and request.url.path == "/web_im/v1/web_im/send_msg"
        if state["final"] == "timeout":
            raise httpx.ReadTimeout("controlled unknown final write")
        if state["final"] == "no_receipt":
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {"msg_key": "456"}})

    adapter = BilibiliMessagesAdapter({"sessdata": "fixture-session", "csrf": "fixture-csrf"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(handle)
    )
    yield adapter, state
    await adapter.close()


def uploads(state):
    return [r for r in state["requests"] if r.url.path == "/x/dynamic/feed/draw/upload_bfs"]


def writes(state):
    return [r for r in state["requests"] if r.url.path == "/web_im/v1/web_im/send_msg"]


@pytest.fixture
async def meme_messages(runtime, settings, store, image_http):
    catalog = MemeCatalog(settings, store)
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    adapter, state = image_http
    asset = await catalog.register(
        "whale",
        state["content"],
        title="角色开心表情",
        emotions=["cheerful"],
        source={"kind": "user_upload", "author": "test operator"},
        license={"name": "fixture only"},
    )
    comments = Comments(settings, store, runtime)
    messages = DirectMessages(settings, store, runtime, comments, adapter=adapter, memes=catalog)
    comments.direct_messages = messages
    await messages.configure({"enabled": True})
    await messages.open()
    yield messages, catalog, asset, state
    await messages.close()
    await comments.close()


async def prepared(bundle, *, image=True, automatic=False, account_id=""):
    messages, catalog, asset, _ = bundle
    source, _ = messages.ingest(
        {
            "message_id": "received-current-user",
            "platform_user_id": "123",
            "display_name": "鱼宝",
            "text": "你好呀",
            "timestamp": time.time(),
            "account_id": account_id,
        }
    )
    identity = messages.enqueue("message", source, automatic=automatic)
    async with asyncio.timeout(4):
        while True:
            row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
            if row["status"] not in {"queued", "generating", "publishing"}:
                break
            await asyncio.sleep(0.005)
    if image:
        messages.review(identity, "edit", meme_id=asset["asset_id"])
        messages.review(identity, "approve")
    return identity


async def test_type2_upload_once_then_one_final_post_keeps_original_gif_bytes(image_http):
    adapter, state = image_http
    prepare, final = [], []

    def prepare_guard():
        prepare.append(True)
        return True

    def final_guard():
        final.append(True)
        return True

    result = await adapter.send_image(
        "123",
        {"content": state["content"], "mime_type": "image/gif"},
        authorized=final_guard,
        prepare_authorized=prepare_guard,
    )
    assert result["msg_key"] == "456" and len(uploads(state)) == len(writes(state)) == 1
    assert prepare == [True] and final == [True]
    multipart = uploads(state)[0].content
    assert state["content"] in multipart and b'filename="meme.gif"' in multipart
    for key, value in [
        ("biz", "new_dyn"),
        ("category", "daily"),
        ("csrf", "fixture-csrf"),
        ("csrf_token", "fixture-csrf"),
    ]:
        assert f'name="{key}"'.encode() in multipart and value.encode() in multipart
    final_request = writes(state)[0]
    fields = parse_qs(final_request.content.decode())
    assert fields["msg[msg_type]"] == ["2"] and fields["msg[receiver_id]"] == ["123"]
    assert fields["msg[sender_uid]"] == ["900"] and "w_rid" in final_request.url.params
    image = json.loads(fields["msg[content]"][0])
    assert image == {
        "url": "https://i0.hdslb.com/bfs/new_dyn/known.gif",
        "height": 384,
        "width": 384,
        "imageType": "gif",
        "original": 1,
        "size": len(state["content"]) / 1024,
    }
    assert "content" not in image  # no hidden second text message


async def test_preparation_and_final_authorization_have_different_write_boundaries(image_http):
    adapter, state = image_http
    allowed = {"value": True}
    state["after_upload"] = lambda: allowed.update(value=False)
    with pytest.raises(ValueError, match="授权"):
        await adapter.send_image(
            "123",
            {"content": state["content"], "mime_type": "image/gif"},
            prepare_authorized=lambda: True,
            authorized=lambda: allowed["value"],
        )
    assert len(uploads(state)) == 1 and not writes(state)


@pytest.mark.parametrize(
    "patch",
    [
        {"image_url": "https://evil.test/a.gif"},
        {"image_url": "https://i0.hdslb.com/bfs/%2e%2e/a.gif"},
        {"image_width": True},
        {"image_height": 0},
        {"img_size": float("inf")},
    ],
)
async def test_bad_upload_receipt_cannot_become_a_picture_message(image_http, patch):
    adapter, state = image_http
    state["receipt_patch"] = patch
    with pytest.raises(ValueError):
        await adapter.send_image("123", {"content": state["content"], "mime_type": "image/gif"})
    assert len(uploads(state)) == 1 and not writes(state)


@pytest.mark.parametrize("mode", ["timeout", "no_receipt"])
async def test_final_unknown_receipt_is_not_an_upload_failure(image_http, mode):
    adapter, state = image_http
    state["final"] = mode
    with pytest.raises(PublishUncertain):
        await adapter.send_image("123", {"content": state["content"], "mime_type": "image/gif"})
    assert len(uploads(state)) == len(writes(state)) == 1


async def test_disabled_or_tampered_catalog_asset_never_reaches_upload(meme_messages):
    messages, catalog, asset, state = meme_messages
    identity = await prepared(meme_messages)
    catalog.enable(asset["asset_id"], False)
    with pytest.raises(ValueError, match="未启用"):
        await messages.send(identity)
    assert not uploads(state) and not writes(state)
    catalog.enable(asset["asset_id"])
    path = catalog.resolve(asset["asset_id"], "whale")["path"]
    content = path.read_bytes()
    path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    with pytest.raises(ValueError, match="完整性"):
        await messages.send(identity)
    assert not uploads(state) and not writes(state)


async def test_upload_failure_is_retriable_without_spending_the_final_intent(meme_messages):
    messages, _, _, state = meme_messages
    identity = await prepared(meme_messages)
    state["upload"] = "failed"
    with pytest.raises(ValueError, match="上传失败"):
        await messages.send(identity)
    assert messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"] == "approved"
    assert not messages.store.all("SELECT * FROM account_writes") and not writes(state)
    # Observe the configured cooldown without deleting an audit record/intent.
    messages.store.execute("UPDATE dm_attempts SET started_at=0 WHERE draft_id=?", (identity,))
    state["upload"] = "ok"
    assert (await messages.send(identity))["status"] == "published"
    assert len(uploads(state)) == 2 and len(writes(state)) == 1


async def test_disabled_after_upload_is_still_rejected_before_the_final_post(meme_messages):
    messages, catalog, asset, state = meme_messages
    identity = await prepared(meme_messages)
    state["after_upload"] = lambda: catalog.enable(asset["asset_id"], False)
    with pytest.raises(ValueError):
        await messages.send(identity)
    assert len(uploads(state)) == 1 and not writes(state)
    assert not messages.store.all("SELECT * FROM account_writes")


async def test_uncertain_image_keeps_pin_and_cannot_be_retried_edited_or_resent(meme_messages):
    messages, catalog, asset, state = meme_messages
    identity = await prepared(meme_messages)
    state["final"] = "timeout"
    with pytest.raises(ValueError, match="待核对"):
        await messages.send(identity)
    assert (
        messages.store.one("SELECT status FROM dm_drafts WHERE id=?", (identity,))["status"]
        == "publish_uncertain"
    )
    for action in (lambda: messages.retry(identity), lambda: messages.review(identity, "edit", meme_id=None)):
        with pytest.raises(ValueError):
            action()
    with pytest.raises(ValueError):
        await messages.send(identity)
    assert len(uploads(state)) == len(writes(state)) == 1
    catalog.enable(asset["asset_id"], False)
    with pytest.raises(ValueError, match="正在"):
        catalog.delete(asset["asset_id"])


async def test_clearing_an_image_reverts_to_one_text_message_and_releases_pin(meme_messages):
    messages, _, _, state = meme_messages
    identity = await prepared(meme_messages)
    before = messages.store.one("SELECT text FROM dm_drafts WHERE id=?", (identity,))["text"]
    assert messages.store.one("SELECT owner FROM meme_pins WHERE owner=?", (f"dm-draft:{identity}",))
    messages.review(identity, "edit", meme_id=None)
    assert not messages.store.one("SELECT owner FROM meme_pins WHERE owner=?", (f"dm-draft:{identity}",))
    messages.review(identity, "approve")
    await messages.send(identity)
    assert not uploads(state) and len(writes(state)) == 1
    fields = parse_qs(writes(state)[0].content.decode())
    assert fields["msg[msg_type]"] == ["1"] and json.loads(fields["msg[content]"][0]) == {"content": before}


@pytest.mark.parametrize("action", ["reject", "publish"])
async def test_image_pins_release_on_rejection_or_proven_success(meme_messages, action):
    messages, _, _, state = meme_messages
    identity = await prepared(meme_messages)
    owner = f"dm-draft:{identity}"
    assert messages.store.one("SELECT * FROM meme_pins WHERE owner=?", (owner,))
    original_text = messages.store.one("SELECT text FROM dm_drafts WHERE id=?", (identity,))["text"]
    if action == "reject":
        messages.review(identity, "reject")
        assert not uploads(state) and not writes(state)
    else:
        await messages.send(identity)
        assert len(uploads(state)) == len(writes(state)) == 1
        assert parse_qs(writes(state)[0].content.decode())["msg[msg_type]"] == ["2"]
        assert (
            messages.store.one("SELECT text FROM dm_drafts WHERE id=?", (identity,))["text"] == original_text
        )
    assert not messages.store.one("SELECT * FROM meme_pins WHERE owner=?", (owner,))


@pytest.mark.parametrize(
    "selection",
    [
        {"asset_id": "https://evil.test/a.gif"},
        {"asset_id": "meme_" + "0" * 32},
        {"asset_ids": ["meme_" + "0" * 32]},
    ],
)
async def test_invalid_selector_preserves_pending_text_and_blocks_auto_send(
    meme_messages, monkeypatch, selection
):
    messages, _, _, state = meme_messages
    await messages.configure({"enabled": True, "auto_send": True, "use_memes": True})

    async def select(*args, **kwargs):
        assert kwargs["max_tokens"] == 2048
        return selection

    monkeypatch.setattr(messages.runtime.llm, "generate_json", select)
    identity = await prepared(meme_messages, image=False, automatic=True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "pending_review" and row["text"] and row["message_kind"] == "text"
    assert json.loads(row["answer_check"])["meme_selection"]["state"] == "failed"
    assert not uploads(state) and not writes(state)


async def test_explicit_null_selector_can_keep_the_existing_automatic_text_path(meme_messages, monkeypatch):
    messages, _, _, state = meme_messages
    await messages.configure({"enabled": True, "auto_send": True, "use_memes": True})

    async def select(*args, **kwargs):
        return {"asset_id": None}

    monkeypatch.setattr(messages.runtime.llm, "generate_json", select)
    identity = await prepared(meme_messages, image=False, automatic=True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "published" and not uploads(state) and len(writes(state)) == 1


async def test_upload_cancellation_is_recoverable_without_a_final_message(meme_messages):
    messages, _, _, state = meme_messages
    identity = await prepared(meme_messages)
    state["upload"] = "block"
    task = asyncio.create_task(messages.send(identity))
    await asyncio.wait_for(state["upload_started"].wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "approved"
    assert not writes(state) and not messages.store.all("SELECT * FROM account_writes")
    assert messages.store.one("SELECT * FROM meme_pins WHERE owner=?", (f"dm-draft:{identity}",))
    assert (
        messages.store.one("SELECT status FROM dm_attempts WHERE draft_id=?", (identity,))["status"]
        == "failed"
    )
    state["upload"] = "ok"
    messages.store.execute(
        "UPDATE dm_attempts SET started_at=? WHERE draft_id=?", (time.time() - 100000, identity)
    )
    await messages.send(identity)
    assert len(uploads(state)) == 2 and len(writes(state)) == 1
    assert not messages.store.one("SELECT * FROM meme_pins WHERE owner=?", (f"dm-draft:{identity}",))


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("selector offline"), RuntimeError("selector failed")])
async def test_selector_service_error_preserves_generated_text_for_review(
    meme_messages, monkeypatch, failure
):
    messages, _, _, state = meme_messages
    await messages.configure({"enabled": True, "auto_send": True, "use_memes": True})

    async def select(*args, **kwargs):
        raise failure

    monkeypatch.setattr(messages.runtime.llm, "generate_json", select)
    identity = await prepared(meme_messages, image=False, automatic=True)
    row = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert row["status"] == "pending_review" and row["text"] and row["message_kind"] == "text"
    assert "表情选择失败" in row["error"]
    assert json.loads(row["answer_check"])["meme_selection"]["state"] == "failed"
    assert not uploads(state) and not writes(state)


@pytest.mark.parametrize("with_image", [False, True])
@pytest.mark.parametrize("action", ["edit", "reject"])
async def test_review_while_account_nav_waits_preserves_latest_draft_before_any_attempt(
    meme_messages, with_image, action
):
    messages, _, _, state = meme_messages
    identity = await prepared(meme_messages, image=with_image, account_id="900")
    if not with_image:
        messages.review(identity, "approve")
    # Use the production account-verification path with the controlled native
    # adapter, retaining its HTTP transport and binding the actual fixture UID.
    messages.injected = False
    messages.comments.credentials = {"sessdata": "fixture-session", "csrf": "fixture-csrf"}
    messages.credential_signature = ("fixture-session", "fixture-csrf")
    state["nav_wait"] = asyncio.Event()
    task = asyncio.create_task(messages.send(identity))
    await asyncio.wait_for(state["nav_entered"].wait(), 2)
    reviewed = messages.review(
        identity, action, text="改过的文字，等重新审核。" if action == "edit" else None
    )
    state["nav_wait"].set()
    with pytest.raises(ValueError):
        await task
    current = messages.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
    assert current["status"] == reviewed["status"] and current["text"] == reviewed["text"]
    assert current["meme_id"] == reviewed["meme_id"] and current["account_id"] == "900"
    assert not uploads(state) and not writes(state)
    assert not messages.store.all("SELECT * FROM dm_attempts")
    assert not messages.store.all("SELECT * FROM account_writes")
    owner = f"dm-draft:{identity}"
    assert bool(messages.store.one("SELECT * FROM meme_pins WHERE owner=?", (owner,))) is (
        with_image and action == "edit"
    )
    messages.review(identity, "approve")
    await messages.send(identity)
    assert len(uploads(state)) == int(with_image) and len(writes(state)) == 1
    assert len(messages.store.all("SELECT * FROM dm_attempts")) == 1
    assert not messages.store.one("SELECT * FROM meme_pins WHERE owner=?", (owner,))
