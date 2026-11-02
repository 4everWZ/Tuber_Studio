"""Public text plus one local image, controlled HTTP and real durable pins."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from tuber_studio.bilibili import BilibiliAdapter
from tuber_studio.dynamics import Dynamics
from tuber_studio.memes import MemeCatalog
from tuber_studio.memory import GroundedMemory

CAPTION = "海底小剧场：今天又开饭啦。\n作者：测试用户"


class Model:
    def __init__(self):
        self.selection = {"asset_id": None}
        self.selector_error = None
        self.requests = []

    async def count_tokens(self, messages):
        return 100

    async def generate_json(self, messages, schema, *, max_tokens):
        self.requests.append((messages, schema, max_tokens))
        payload = json.loads(messages[1]["content"])
        if payload.get("task") == "operations_memory_summary":
            return {
                "entries": [
                    {"source_id": source["id"], "quote": source["text"]}
                    for source in payload["public_sources"]
                ]
            }
        if "asset_id" in schema.get("properties", {}):
            if self.selector_error:
                raise self.selector_error
            return self.selection
        return {
            "action": "post",
            "reason": "公开分享虚构故事",
            "content": CAPTION,
            "source_ids": [payload["public_sources"][0]["id"]],
            "next_evaluation_seconds": 1800,
        }


@pytest.fixture
async def dynamic_memes(settings, store):
    catalog = MemeCatalog(settings, store)
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    gif = (settings.root / "assets/whale/cheerful.gif").read_bytes()
    asset = await catalog.register(
        "whale",
        gif,
        title="开心表情",
        emotions=["cheerful"],
        source={"kind": "user_upload", "author": "test operator"},
        license={"name": "fixture only"},
    )
    state = {
        "requests": [],
        "upload": "ok",
        "final": "ok",
        "after_upload": None,
        "upload_entered": asyncio.Event(),
        "upload_wait": None,
        "final_entered": asyncio.Event(),
        "final_wait": None,
        "nav": "ok",
        "nav_entered": asyncio.Event(),
        "nav_wait": None,
    }

    async def respond(request):
        state["requests"].append(request)
        if request.url.path == "/x/web-interface/nav":
            state["nav_entered"].set()
            if state["nav_wait"]:
                await state["nav_wait"].wait()
            if state["nav"] == "failed":
                return httpx.Response(503, json={"code": -1})
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
            state["upload_entered"].set()
            if state["upload_wait"]:
                await state["upload_wait"].wait()
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
                        "img_size": len(gif) / 1024,
                    },
                },
            )
        assert request.url.path == "/x/dynamic/feed/create/dyn"
        state["final_entered"].set()
        if state["final_wait"]:
            await state["final_wait"].wait()
        if state["final"] == "timeout":
            raise httpx.ReadTimeout("controlled final write uncertainty")
        return httpx.Response(
            200,
            json={"code": 0, "data": {} if state["final"] == "no_receipt" else {"dyn_id_str": "987654321"}},
        )

    adapter = BilibiliAdapter({"sessdata": "fixture-session", "csrf": "fixture-csrf"})
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        base_url="https://api.bilibili.com", transport=httpx.MockTransport(respond)
    )
    comments = SimpleNamespace(adapter=adapter, publish_lock=asyncio.Lock(), configured=True)
    comments.policy = lambda: {"credentials_configured": comments.configured}

    async def account():
        return "900"

    comments.account_id = account
    runtime = SimpleNamespace(llm=Model(), generation_lock=asyncio.Lock())
    service = Dynamics(settings, store, runtime, comments, memes=catalog)
    GroundedMemory(store).record(
        "public:test",
        store.character(),
        "event-public-story",
        "assistant",
        "在虚构的海底厨房里，我又多盛了一碗饭。",
        kind="role_story",
    )
    memory = store.one("SELECT MAX(id) AS id FROM grounded_memory")["id"]
    service.mark_memory(memory, True)
    yield service, catalog, asset, state, memory
    await service.close()
    await adapter.close()


def uploads(state):
    return [request for request in state["requests"] if request.url.path == "/x/dynamic/feed/draw/upload_bfs"]


def posts(state):
    return [request for request in state["requests"] if request.url.path == "/x/dynamic/feed/create/dyn"]


async def draft(bundle, *, image=True):
    service, _, asset, _, _ = bundle
    decision = await service.evaluate(manual=True)
    identity = decision["post_id"]
    if image:
        service.review(identity, "edit", meme_id=asset["asset_id"])
    service.review(identity, "approve")
    return identity


async def test_text_plus_one_image_has_one_final_scene2_post_and_keeps_caption(dynamic_memes):
    service, catalog, asset, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    assert service.store.one("SELECT * FROM meme_pins WHERE owner=?", (f"dynamic-draft:{identity}",))
    result = await service.publish(identity)
    assert (
        result["status"] == "published"
        and result["content"] == CAPTION
        and result["meme_id"] == asset["asset_id"]
    )
    assert len(uploads(state)) == len(posts(state)) == 1
    payload = json.loads(posts(state)[0].content)["dyn_req"]
    assert payload["scene"] == 2 and payload["content"]["contents"] == [
        {"raw_text": CAPTION, "biz_id": "", "type": 1}
    ]
    assert payload["pics"] == [
        {"img_src": "https://i0.hdslb.com/bfs/new_dyn/known.gif", "img_width": 384, "img_height": 384}
    ]
    assert "w_rid" in posts(state)[0].url.params
    assert len(service.store.all("SELECT * FROM account_writes")) == 1
    assert not service.store.one("SELECT * FROM meme_pins WHERE owner=?", (f"dynamic-draft:{identity}",))
    assert catalog.read(asset["asset_id"], "whale")["asset"]["animated"]


async def test_review_omission_retains_image_null_clears_and_text_path_stays_scene1(dynamic_memes):
    service, _, asset, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    assert service.review(identity, "approve")["meme_id"] == asset["asset_id"]
    with pytest.raises(ValueError, match="edit"):
        service.review(identity, "approve", meme_id=None)
    cleared = service.review(identity, "edit", meme_id=None)
    assert (
        cleared["status"] == "pending_review" and cleared["content"] == CAPTION and cleared["meme_id"] is None
    )
    assert not service.store.all("SELECT * FROM meme_pins")
    service.review(identity, "approve")
    await service.publish(identity)
    assert not uploads(state) and len(posts(state)) == 1
    payload = json.loads(posts(state)[0].content)["dyn_req"]
    assert payload["scene"] == 1 and "pics" not in payload


async def test_disabled_image_cannot_upload_and_rejected_image_releases_pin(dynamic_memes):
    service, catalog, asset, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    catalog.enable(asset["asset_id"], False)
    with pytest.raises(ValueError):
        await service.publish(identity)
    assert not uploads(state) and not posts(state)
    service.review(identity, "reject")
    assert not service.store.all("SELECT * FROM meme_pins")
    assert catalog.delete(asset["asset_id"])["deleted"]


async def test_image_draft_cannot_be_copied_as_successful_plain_text(dynamic_memes):
    service, _, _, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    with pytest.raises(ValueError, match="图文"):
        service.review(identity, "copy")
    assert not posts(state) and service._post(identity)["status"] == "approved"


async def test_upload_failure_spends_no_intent_and_can_be_reviewed_and_retried(dynamic_memes):
    service, _, _, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    state["upload"] = "failed"
    with pytest.raises(ValueError, match="准备失败"):
        await service.publish(identity)
    assert service._post(identity)["status"] == "approved" and service._post(identity)["attempted_at"] is None
    assert not posts(state) and not service.store.all("SELECT * FROM account_writes")
    service.review(identity, "approve")
    state["upload"] = "ok"
    assert (await service.publish(identity))["status"] == "published"
    assert len(uploads(state)) == 2 and len(posts(state)) == 1


@pytest.mark.parametrize("with_image", [False, True])
@pytest.mark.parametrize("failure", ["value_error", "cancelled"])
async def test_account_verification_failure_preserves_approved_draft_and_allows_retry(
    dynamic_memes, with_image, failure
):
    service, _, asset, state, _ = dynamic_memes
    identity = await draft(dynamic_memes, image=with_image)

    async def account():
        nav = await service.comments.adapter._get("/x/web-interface/nav", {})
        return str(nav["mid"])

    service.comments.account_id = account
    if failure == "value_error":
        state["nav"] = "failed"
    else:
        state["nav_wait"] = asyncio.Event()
    task = asyncio.create_task(service.publish(identity))
    await asyncio.wait_for(state["nav_entered"].wait(), 2)
    if failure == "cancelled":
        task.cancel()
    with pytest.raises(ValueError if failure == "value_error" else asyncio.CancelledError):
        await task
    row = service._post(identity)
    assert row["status"] == "approved" and row["attempted_at"] is None
    assert row["content"] == CAPTION
    assert row["meme_id"] == (asset["asset_id"] if with_image else None)
    assert not uploads(state) and not posts(state)
    assert not service.store.all("SELECT * FROM account_writes")
    pin = service.store.one("SELECT * FROM meme_pins WHERE owner=?", (f"dynamic-draft:{identity}",))
    assert bool(pin) is with_image
    state["nav"], state["nav_wait"] = "ok", None
    assert (await service.publish(identity))["status"] == "published"
    assert len(uploads(state)) == int(with_image) and len(posts(state)) == 1
    assert len(service.store.all("SELECT * FROM account_writes")) == 1
    assert not service.store.one("SELECT * FROM meme_pins WHERE owner=?", (f"dynamic-draft:{identity}",))


@pytest.mark.parametrize("action", ["edit", "reject"])
async def test_review_during_account_verification_preserves_latest_operator_decision(dynamic_memes, action):
    service, _, _, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    state["nav_wait"] = asyncio.Event()

    async def account():
        return str((await service.comments.adapter._get("/x/web-interface/nav", {}))["mid"])

    service.comments.account_id = account
    task = asyncio.create_task(service.publish(identity))
    await asyncio.wait_for(state["nav_entered"].wait(), 2)
    reviewed = service.review(identity, action, text=CAPTION + " 再添一碗。" if action == "edit" else None)
    state["nav_wait"].set()
    with pytest.raises(ValueError):
        await task
    current = service._post(identity)
    assert current["status"] == reviewed["status"] and current["content"] == reviewed["content"]
    assert current["meme_id"] == reviewed["meme_id"] and current["attempted_at"] is None
    assert not uploads(state) and not posts(state) and not service.store.all("SELECT * FROM account_writes")
    owner = f"dynamic-draft:{identity}"
    assert bool(service.store.one("SELECT * FROM meme_pins WHERE owner=?", (owner,))) is (action == "edit")
    service.review(identity, "approve")
    assert (await service.publish(identity))["status"] == "published"
    assert len(uploads(state)) == len(posts(state)) == 1


@pytest.mark.parametrize("change", ["disable", "account", "source", "role"])
async def test_upload_then_changed_authorization_never_submits(dynamic_memes, change):
    service, catalog, asset, state, memory = dynamic_memes
    identity = await draft(dynamic_memes)

    def mutate():
        if change == "disable":
            catalog.enable(asset["asset_id"], False)
        elif change == "account":
            service.comments.adapter = object()
        elif change == "source":
            service.mark_memory(memory, False)
        else:
            service.store.save_character(service.store.character())

    state["after_upload"] = mutate
    with pytest.raises(ValueError):
        await service.publish(identity)
    assert len(uploads(state)) == 1 and not posts(state)
    assert (
        not service.store.all("SELECT * FROM account_writes")
        and service._post(identity)["attempted_at"] is None
    )


@pytest.mark.parametrize("mode", ["timeout", "no_receipt"])
async def test_final_unknown_keeps_pin_pauses_and_blocks_republish_after_restart(dynamic_memes, mode):
    service, catalog, asset, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    state["final"] = mode
    with pytest.raises(ValueError, match="不确定"):
        await service.publish(identity)
    assert service._post(identity)["status"] == "publish_uncertain"
    assert service.store.all("SELECT * FROM meme_pins") and len(posts(state)) == 1
    again = Dynamics(service.settings, service.store, service.runtime, service.comments, memes=catalog)
    for action in (
        lambda: again.review(identity, "edit", meme_id=None),
        lambda: again.review(identity, "approve"),
    ):
        with pytest.raises(ValueError):
            action()
    with pytest.raises(ValueError):
        await again.publish(identity)
    assert len(uploads(state)) == len(posts(state)) == 1
    catalog.enable(asset["asset_id"], False)
    with pytest.raises(ValueError, match="正在"):
        catalog.delete(asset["asset_id"])


async def test_cancel_during_preparation_is_not_a_final_write_uncertainty(dynamic_memes):
    service, _, _, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    state["upload_wait"] = asyncio.Event()
    task = asyncio.create_task(service.publish(identity))
    await state["upload_entered"].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._post(identity)["status"] == "approved" and service._post(identity)["attempted_at"] is None
    assert not posts(state) and not service.store.all("SELECT * FROM account_writes")


async def test_cancel_during_final_post_is_uncertain_and_retains_pin(dynamic_memes):
    service, _, _, state, _ = dynamic_memes
    identity = await draft(dynamic_memes)
    state["final_wait"] = asyncio.Event()
    task = asyncio.create_task(service.publish(identity))
    await state["final_entered"].wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service._post(identity)["status"] == "publish_uncertain" and service.store.all(
        "SELECT * FROM meme_pins"
    )


@pytest.mark.parametrize("bad", ["foreign_asset", "model_error"])
async def test_invalid_selector_keeps_caption_pending_and_does_not_auto_publish(dynamic_memes, bad):
    service, _, _, state, _ = dynamic_memes
    await service.set_policy({"enabled": True, "auto_publish": True, "use_memes": True})
    service.runtime.llm.selection = {"asset_id": "meme_" + "0" * 32}
    if bad == "model_error":
        service.runtime.llm.selector_error = RuntimeError("controlled model failure")
    decision = await service.evaluate()
    row = service._post(decision["post_id"])
    assert row["status"] == "pending_review" and row["content"] == CAPTION and row["meme_id"] is None
    assert "选择失败" in row["error"] and not uploads(state) and not posts(state)


async def test_dynamic_selector_attaches_to_the_unchanged_caption(dynamic_memes):
    service, _, asset, state, _ = dynamic_memes
    assert service.policy()["use_memes"] is False
    await service.set_policy({"use_memes": True})
    service.runtime.llm.selection = {"asset_id": asset["asset_id"]}
    decision = await service.evaluate(manual=True)
    row = service._post(decision["post_id"])
    assert row["meme_id"] == asset["asset_id"] and row["content"] == CAPTION and not posts(state)
    calls = [call for call in service.runtime.llm.requests if "asset_id" in call[1].get("properties", {})]
    assert len(calls) == 1 and calls[0][2] == 2048
    assert json.loads(calls[0][0][1]["content"])["mode"] == "dynamic"


@pytest.mark.parametrize("with_image", [False, True])
async def test_valid_automatic_selection_posts_once_with_original_caption(dynamic_memes, with_image):
    service, _, asset, state, _ = dynamic_memes
    await service.set_policy({"enabled": True, "auto_publish": True, "use_memes": True})
    if with_image:
        service.runtime.llm.selection = {"asset_id": asset["asset_id"]}
    result = await service.evaluate()
    row = service._post(result["post_id"])
    assert row["status"] == "published" and row["content"] == CAPTION
    assert len(posts(state)) == 1 and len(uploads(state)) == int(with_image)
    body = json.loads(posts(state)[0].content)["dyn_req"]
    assert body["scene"] == (2 if with_image else 1)
    assert body["content"]["contents"][0]["raw_text"] == CAPTION
    assert not service.store.all("SELECT * FROM meme_pins")
