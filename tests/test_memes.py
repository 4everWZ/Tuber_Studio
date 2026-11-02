import asyncio
import base64
import hashlib
import json
import shutil
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.browsing_models import BrowsingPolicy
from tuber_studio.browsing_store import BrowsingRepository
from tuber_studio.generation_meter import GenerationMeter
from tuber_studio.meme_selection import select_meme
from tuber_studio.memes import MAX_BYTES, ImageInspector, MemeCatalog
from tuber_studio.providers import LlamaCppProvider

SOURCE = {
    "kind": "user_upload",
    "author": "operator",
    "note": "User supplied image; no remote image was downloaded",
}
LICENSE = {"name": "user supplied; rights not independently verified"}


@pytest.fixture
def catalog(settings, store):
    return MemeCatalog(settings, store)


@pytest.fixture
def image(settings):
    return (settings.root / "assets/whale/sing_open.png").read_bytes()


async def register(catalog, image, **overrides):
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装；非解码边界测试仍运行")
    args = {
        "title": "existing local test image",
        "emotions": ["cheerful"],
        "source": SOURCE,
        "license": LICENSE,
    }
    return await catalog.register("whale", image, **(args | overrides))


async def test_native_decode_register_preview_read_and_role_selection(catalog, image):
    asset = await register(catalog, image)
    assert asset["mime_type"] == "image/png" and (asset["width"], asset["height"], asset["frames"]) == (
        384,
        384,
        1,
    )
    assert asset["sha256"] == hashlib.sha256(image).hexdigest() and not asset["animated"]
    assert asset["source"] == SOURCE | {"url": "", "revision": ""}
    assert asset["license"]["name"] == LICENSE["name"] and "path" not in asset
    assert catalog.read(asset["asset_id"], "whale")["content"] == image
    assert catalog.select({"asset_id": asset["asset_id"]}, "whale")["asset_id"] == asset["asset_id"]
    assert catalog.resolve(asset["asset_id"], "whale")["path"].read_bytes() == image
    assert len(catalog.list("whale")) == 1
    assert asset["asset_id"] in json.dumps(catalog.selection_schema("whale"))


async def test_original_gif_bytes_frames_and_license_are_preserved(catalog, settings):
    original = (settings.root / "assets/whale/cheerful.gif").read_bytes()
    asset = await register(catalog, original)
    assert asset["mime_type"] == "image/gif" and asset["animated"] and asset["frames"] == 5
    assert catalog.read(asset["asset_id"], "whale")["content"] == original
    assert catalog.resolve(asset["asset_id"], "whale")["path"].suffix == ".gif"


async def test_disabled_assets_are_manageable_but_not_selectable(catalog, image):
    asset = await register(catalog, image, enabled=False)
    assert catalog.list("whale") == []
    assert len(catalog.list("whale", include_disabled=True)) == 1
    assert catalog.read(asset["asset_id"], "whale", allow_disabled=True)["content"] == image
    with pytest.raises(ValueError, match="未启用"):
        catalog.read(asset["asset_id"], "whale")
    with pytest.raises(ValueError):
        catalog.select({"asset_id": asset["asset_id"]}, "whale")
    catalog.enable(asset["asset_id"])
    assert catalog.select({"asset_id": asset["asset_id"]}, "whale")


async def test_role_binding_is_required_even_for_management_preview(catalog, image, store):
    asset = await register(catalog, image)
    other = next(c["id"] for c in store.characters() if c["id"] != "whale")
    assert catalog.list(other) == []
    with pytest.raises(ValueError):
        catalog.read(asset["asset_id"], other, allow_disabled=True)
    catalog.bind(asset["asset_id"], other)
    assert catalog.read(asset["asset_id"], other)["content"] == image
    catalog.bind(asset["asset_id"], other, enabled=False)
    with pytest.raises(ValueError):
        catalog.read(asset["asset_id"], other)
    assert catalog.read(asset["asset_id"], "whale")["content"] == image


@pytest.mark.parametrize(
    "value",
    ["https://example.com/pic.gif", "../../studio.json", "C:/Windows/x.png", "file:///secret", "meme_bad"],
)
def test_model_urls_and_arbitrary_paths_are_never_assets(catalog, value):
    with pytest.raises(ValueError):
        catalog.read(value, "whale")
    with pytest.raises(ValueError):
        catalog.select({"asset_id": value}, "whale")


async def test_only_one_offered_asset_or_text_only_is_allowed(catalog, image):
    asset = await register(catalog, image)
    assert catalog.select(None, "whale") is None and catalog.select({"asset_id": None}, "whale") is None
    for selection in (
        [asset["asset_id"]],
        {"asset_ids": [asset["asset_id"]]},
        {"asset_id": asset["asset_id"], "url": "https://evil.test"},
    ):
        with pytest.raises(ValueError):
            catalog.select(selection, "whale")
    with pytest.raises(ValueError, match="本次"):
        catalog.select({"asset_id": asset["asset_id"]}, "whale", offered_ids=[])


@pytest.mark.parametrize("value", [b"", b"not an image", b"<svg></svg>", b"GIF89a" + b"x" * MAX_BYTES])
async def test_bounded_real_image_bytes_required_before_native_process(catalog, value):
    with pytest.raises(ValueError):
        await catalog.register("whale", value, title="x", emotions=["idle"], source=SOURCE, license=LICENSE)
    assert not catalog.list("whale", include_disabled=True) and not list(catalog.library.iterdir())


async def test_mime_mismatch_and_header_only_fake_image_never_register(catalog, image):
    with pytest.raises(ValueError, match="MIME"):
        await catalog.register(
            "whale",
            image,
            title="x",
            emotions=["idle"],
            source=SOURCE,
            license=LICENSE,
            mime_type="image/gif",
        )
    if not catalog.inspector.binary:
        return
    with pytest.raises(ValueError):
        await register(catalog, b"\x89PNG\r\n\x1a\ntruncated")
    assert not catalog.list("whale", include_disabled=True)


@pytest.mark.parametrize(
    "patch",
    [
        {"emotions": ["not-an-emotion"]},
        {"emotions": ["idle", "idle"]},
        {"emotions": []},
        {"source": SOURCE | {"url": "file:///secret"}},
        {"license": {}},
        {"title": "bad\x00title"},
    ],
)
async def test_invalid_metadata_cannot_persist_claims(catalog, image, patch):
    with pytest.raises(ValueError):
        await register(catalog, image, **patch)
    assert not catalog.list("whale", include_disabled=True)


async def test_same_size_tampering_is_rejected_by_resolve_and_read(catalog, image):
    asset = await register(catalog, image)
    path = catalog.resolve(asset["asset_id"], "whale")["path"]
    path.write_bytes(image[:-1] + bytes([image[-1] ^ 1]))
    for method in (catalog.resolve, catalog.read):
        with pytest.raises(ValueError, match="完整性"):
            method(asset["asset_id"], "whale")


async def test_symlink_blob_and_directory_are_rejected(catalog, image, tmp_path):
    asset = await register(catalog, image)
    path = catalog.resolve(asset["asset_id"], "whale")["path"]
    target = tmp_path / "unrelated.png"
    target.write_bytes(image)
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("本机没有创建symlink权限")
    with pytest.raises(ValueError, match="链接"):
        catalog.read(asset["asset_id"], "whale")
    assert target.read_bytes() == image


async def test_disabled_delete_pins_and_owner_release_survive_reopening(catalog, image, settings, store):
    first = await register(catalog, image)
    second = await register(catalog, image, title="second")
    catalog.pin(first["asset_id"], "whale", "dm:1")
    catalog.pin(second["asset_id"], "whale", "dm:1")
    catalog.pin(second["asset_id"], "whale", "dm:2")
    reopened = MemeCatalog(settings, store)
    reopened.release("dm:1", keep_asset_id=first["asset_id"])
    assert {(r["asset_id"], r["owner"]) for r in store.all("SELECT * FROM meme_pins")} == {
        (first["asset_id"], "dm:1"),
        (second["asset_id"], "dm:2"),
    }
    reopened.enable(first["asset_id"], False)
    with pytest.raises(ValueError, match="正在"):
        reopened.delete(first["asset_id"])
    reopened.release("dm:1")
    assert reopened.delete(first["asset_id"])["deleted"]
    assert reopened.read(second["asset_id"], "whale")["content"] == image  # shared blob retained
    with pytest.raises(ValueError, match="禁用"):
        reopened.delete(second["asset_id"])
    reopened.enable(second["asset_id"], False)
    reopened.release("dm:2")
    assert reopened.delete(second["asset_id"])["deleted"]
    assert not list(catalog.library.iterdir())


async def test_expired_pin_does_not_block_explicit_deletion(catalog, image, store):
    asset = await register(catalog, image)
    catalog.pin(asset["asset_id"], "whale", "short", seconds=1)
    store.execute("UPDATE meme_pins SET expires_at=0")
    catalog.enable(asset["asset_id"], False)
    assert catalog.delete(asset["asset_id"])["deleted"]


async def test_bootstrap_preserves_disabled_deleted_preferences_and_original_files(catalog, settings, store):
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    original = {p.name: p.read_bytes() for p in (settings.root / "assets/whale").glob("*.gif")}
    rows = await catalog.seed_existing()
    assert len(rows) == 6 and all(not row["enabled"] and row["animated"] for row in rows)
    row = rows[0]
    assert row["source"]["kind"] == "existing_user_assets" and row["source"]["author"] == "YunYueSama"
    assert row["license"]["name"] == "大肥鱼项目署名许可 1.0" and "第三方" in row["license"]["note"]
    assert (
        catalog.read(row["asset_id"], "whale", allow_disabled=True)["content"]
        == original[row["title"] + ".gif"]
    )
    catalog.delete(row["asset_id"])
    reopened = MemeCatalog(settings, store)
    await reopened.seed_existing(enabled=True)
    assert len(reopened.list("whale", include_disabled=True)) == 5
    assert not reopened.list("whale")  # bootstrapping never flips existing disabled choices
    assert {p.name: p.read_bytes() for p in (settings.root / "assets/whale").glob("*.gif")} == original


async def test_registration_database_failure_does_not_leave_original_or_partial_file(
    catalog, image, monkeypatch
):
    def fail(*args, **kwargs):
        raise ValueError("injected DB failure")

    monkeypatch.setattr(catalog, "_insert", fail)
    with pytest.raises(ValueError, match="DB failure"):
        await register(catalog, image)
    assert not list(catalog.library.iterdir())


async def test_no_decoder_is_an_explicit_error_not_header_only_success(catalog, image):
    catalog.inspector.binary = None
    with pytest.raises(ValueError, match="ffprobe"):
        await catalog.register("whale", image, title="x", emotions=["idle"], source=SOURCE, license=LICENSE)
    assert not list(catalog.library.iterdir())


async def test_cancelled_native_validation_reaps_owned_process(monkeypatch):
    inspector = ImageInspector()
    if not inspector.binary:
        pytest.skip("真实ffprobe未安装")
    processes = []
    original = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        # Exercise a real long-running native child instead of a fake process.
        process = await original(
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=16x16:r=1",
            "-f",
            "null",
            "-",
            **kwargs,
        )
        processes.append(process)
        return process

    if not shutil.which("ffmpeg"):
        pytest.skip("真实ffmpeg未安装")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(inspector.inspect(b"GIF89aheader"))
    for _ in range(200):
        if processes:
            break
        await asyncio.sleep(0.005)
    assert processes
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(process.returncode is not None for process in processes)


def test_database_storage_path_is_not_trusted(catalog, store):
    # An invalid asset ID is rejected before any path read; stored locators have
    # a separate fixed allowlist as defense against a damaged local database.
    fake = {"storage_kind": "library", "blob_name": "../../studio.json"}
    with pytest.raises(ValueError, match="存储"):
        catalog._path(fake)
    fake["storage_kind"] = "existing_user_assets"
    with pytest.raises(ValueError, match="存储"):
        catalog._path(fake)


async def test_selector_inherits_parent_budget_and_counts_actual_native_usage(
    catalog, image, settings, store
):
    asset = await register(catalog, image)
    repository = BrowsingRepository(store)
    parent, _ = repository.create_round(
        "900", store.character(), BrowsingPolicy(max_model_calls=1).model_dump(), 0, True
    )
    calls = []

    def respond(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3, 4]})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["chat_template_kwargs"]["enable_thinking"] is True
        assert body["max_tokens"] == 2048
        assert repository.round(parent["id"])["budget"]["model_calls"] == 1
        calls.append(body)
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 4, "completion_tokens": 11},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"asset_id": asset["asset_id"]})},
                    }
                ],
            },
        )

    model = LlamaCppProvider(settings.data["llm"] | {"enable_thinking": True})
    await model.client.aclose()
    model.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    runtime = SimpleNamespace(llm=model)
    try:
        with GenerationMeter(repository, parent["id"]):
            selected = await select_meme(runtime, catalog, store.character(), {"text": "谢谢"}, "不客气呀")
            assert selected["asset_id"] == asset["asset_id"]
            with pytest.raises(ValueError, match="预算耗尽"):
                await select_meme(runtime, catalog, store.character(), {"text": "你好"}, "你好呀")
        assert len(calls) == 1
        budget = repository.round(parent["id"])["budget"]
        assert budget["model_calls"] == 1 and budget["input_tokens"] == 4 and budget["output_tokens"] == 11
    finally:
        await model.close()


def test_management_dto_and_disable_are_scoped_to_character(settings):
    class Platform:
        async def close(self):
            pass

    app = create_app(settings, Platform())
    with TestClient(app) as client:
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        assert app.state.messages.memes is app.state.memes is app.state.dynamics.memes
        item = client.get("/api/memes?character=whale").json()["items"][0]
        identity = item["asset_id"]
        other = next(c["id"] for c in app.state.store.characters() if c["id"] != "whale")
        for character in ("whale", other):
            result = client.put(f"/api/memes/{identity}", json={"character": character, "enabled": True})
            assert result.status_code == 200 and result.json()["enabled"]
        disabled = client.put(f"/api/memes/{identity}", json={"character": "whale", "enabled": False})
        assert disabled.status_code == 200 and not disabled.json()["enabled"]
        for character, expected in (("whale", False), (other, True)):
            result = client.get("/api/memes", params={"character": character}).json()
            current = next(row for row in result["items"] if row["asset_id"] == identity)
            assert current["enabled"] is expected
        assert client.get(f"/api/memes/{identity}/preview?character=whale").status_code == 200
        with pytest.raises(ValueError, match="未启用"):
            app.state.memes.read(identity, "whale")
        assert app.state.memes.read(identity, other)["content"]
        assert client.delete(f"/api/memes/{identity}?character=whale").status_code == 400
        client.put(f"/api/memes/{identity}", json={"character": other, "enabled": False}).raise_for_status()
        assert client.delete(f"/api/memes/{identity}?character=whale").json()["deleted"]


def test_management_can_delete_a_missing_unpinned_library_entry(settings):
    class Platform:
        async def close(self):
            pass

    app = create_app(settings, Platform())
    with TestClient(app) as client:
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        content = (settings.root / "assets/whale/sing_open.png").read_bytes()
        result = client.post(
            "/api/memes",
            json={
                "character": "whale",
                "title": "uploaded temporary fixture",
                "emotions": ["cheerful"],
                "source": SOURCE,
                "license": LICENSE,
                "content_base64": base64.b64encode(content).decode(),
            },
        )
        result.raise_for_status()
        identity = result.json()["asset_id"]
        path = app.state.memes.resolve(identity, "whale")["path"]
        client.put(f"/api/memes/{identity}", json={"character": "whale", "enabled": False}).raise_for_status()
        path.unlink()
        listing = client.get("/api/memes?character=whale").json()["items"]
        assert next(item for item in listing if item["asset_id"] == identity)["missing"]
        deleted = client.delete(f"/api/memes/{identity}?character=whale")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"]
        assert identity not in {
            item["asset_id"] for item in client.get("/api/memes?character=whale").json()["items"]
        }
