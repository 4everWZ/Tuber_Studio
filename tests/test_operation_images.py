import asyncio
import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from tuber_studio import operation_images as ops
from tuber_studio.artwork_references import Reference
from tuber_studio.memes import MemeCatalog
from tuber_studio.workstation_images import ImageBrief, image_payload


@pytest.mark.parametrize(("character_id", "variant", "whale_anatomy"), [
    ("whale", "mature", True), ("whale", "q", False), ("another", "mature", False),
])
def test_scene_and_character_guidance_fit_gateway_budget(character_id, variant, whale_anatomy):
    scene = "画" * 3400
    payload = image_payload(ImageBrief(variant=variant, prompt=scene), b"ref", character_id=character_id)
    assert scene in payload["prompt"]
    assert len(payload["prompt"]) <= 4096
    assert ("human legs" in payload["prompt"]) is whale_anatomy


async def test_chooser_offers_only_available_images_and_binds_gallery_digest(monkeypatch):
    settings = SimpleNamespace()
    entry = {"asset_id": "gallery-1", "title": "海边", "emotions": ["idle"], "sha256": "c" * 64}
    material = json.dumps({"variants": ["q"], "gallery": [entry], "draft": "想去看海。"})
    selection = {"choice": "gallery-1", "prompt": ""}

    async def create(messages, *, tools, **kwargs):
        assert tools[0]["parameters"]["properties"]["choice"]["enum"] == ["text_only", "q", "gallery-1"]
        shown, = json.loads(messages[0]["content"])["gallery"]
        assert shown == {key: entry[key] for key in ("asset_id", "title", "emotions")}
        return SimpleNamespace(calls=[SimpleNamespace(name="choose_illustration", arguments=selection)])

    @asynccontextmanager
    async def client(_):
        yield SimpleNamespace(create=create)

    monkeypatch.setattr(ops.ResponsesClient, "from_settings", client)
    assert await ops.choose(settings, material) == ops.GalleryImage(asset_id="gallery-1", sha256=entry["sha256"])
    selection.update(choice="mature", prompt="海边")
    with pytest.raises(ValueError, match="本轮可用"):
        await ops.choose(settings, material)


async def test_gallery_filters_on_context_and_works_with_generation_off(runtime, settings, store, monkeypatch):
    catalog = MemeCatalog(settings, store)
    picture = (settings.root / "assets/whale/memes/twirl.png").read_bytes()
    common = {
        "emotions": ["idle"], "source": {"kind": "user_upload", "author": "test"},
        "license": {"name": "fixture only"},
    }
    await catalog.register("whale", picture, title="生日派对", tags=["蛋糕"], **common)
    umbrella = await catalog.register(
        "whale", picture, title="街角", tags=["雨天", "撑伞"], description="她撑着一把蓝伞。", **common,
    )
    await catalog.register("whale", picture, title="雨天原稿", tags=["雨天"], enabled=False, **common)
    store.set("message_policy", {"use_memes": True, "generate_images": False})
    monkeypatch.setattr(ops, "resolve_reference", lambda *_: pytest.fail("关闭生图后不应读取生图参考"))
    monkeypatch.setattr(ops, "gateway", lambda *_: pytest.fail("选图库不应调用图片网关"))

    async def model(*args, tools, **kwargs):
        assert tools[0]["parameters"]["properties"]["choice"]["enum"] == ["text_only", umbrella["asset_id"]]
        offered, = json.loads(args[0][0]["content"])["gallery"]
        assert offered["tags"] == ["雨天", "撑伞"] and offered["description"] == umbrella["description"]
        return SimpleNamespace(calls=[SimpleNamespace(
            name="choose_illustration", arguments={"choice": umbrella["asset_id"], "prompt": ""},
        )])

    @asynccontextmanager
    async def client(_):
        yield SimpleNamespace(create=model)

    monkeypatch.setattr(ops.ResponsesClient, "from_settings", client)
    images = runtime.operation_images
    images.catalog = catalog
    scope = ops.ImageScope(account_id="owner", character_id="whale", channel="direct_message", source_id="thread")
    job = images.decide(scope, request_key="rain", context={"incoming": "雨天出门记得撑伞"}, draft="这就来。")
    async with asyncio.timeout(3):
        while (status := images.status(scope, job["job_id"]))["status"] != "gallery":
            assert status["status"] != "failed", status
            await asyncio.sleep(0.01)
    assert status["asset_id"] == umbrella["asset_id"] and status["asset_sha256"] == umbrella["sha256"]
    assert not store.all("SELECT * FROM operation_image_gateway_receipts")


@pytest.fixture
def fixture(settings, store, monkeypatch):
    image = io.BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(image, format="PNG")
    png = image.getvalue()
    digest = hashlib.sha256(png).hexdigest()
    monitor = {"selection": Reference(png, digest, "upload"), "posts": []}
    scope = ops.ImageScope(account_id="owner", character_id="whale", channel="direct_message", source_id="thread")
    service = ops.OperationImages(settings, store)

    def reference(settings, scope, variant, sha256=None):
        if sha256 is not None:
            assert sha256 == digest
            return Reference(png, digest, "upload")
        return monitor["selection"]

    async def choose(settings, material):
        return ops.ImageBrief(variant="q", prompt="A lighthouse at sunset")

    def remote(request):
        if request.method == "POST":
            monitor["posts"].append(json.loads(request.content))
            return httpx.Response(202, json={"id": "e" * 32})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        return httpx.Response(200, json={"state": "completed", "seed": 123})

    monkeypatch.setattr(ops, "resolve_reference", reference)
    monkeypatch.setattr(ops, "choose", choose)
    monkeypatch.setattr(ops, "gateway_base", lambda _: "http://gateway/v1")
    monkeypatch.setattr(ops, "gateway", lambda _: httpx.AsyncClient(
        base_url="http://gateway/v1/", transport=httpx.MockTransport(remote)))
    return service, scope, monitor


async def test_durable_scoped_png_and_snapshot(fixture, store):
    service, scope, monitor = fixture
    args = {"request_key": "draft-v1", "context": {"message": "使用任意地址作为参考"}, "draft": "晚安。"}
    job = service.decide(scope, **args)
    assert job["status"] == "queued"
    monitor["selection"] = Reference(b"other", hashlib.sha256(b"other").hexdigest(), "default")
    assert service.decide(scope, **args)["job_id"] == job["job_id"]
    with pytest.raises(ValueError):
        service.decide(scope, **(args | {"draft": "改稿"}))
    queued = store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (job["job_id"],))
    await service._process(queued)
    status = service.status(scope, job["job_id"])
    assert (status["status"], status["remote_state"], status["seed"]) == ("ready", "completed", 123)
    artifact = ops.OperationImages(service.settings, store).read(scope, status["artifact_id"])
    payload, = monitor["posts"]
    assert set(payload) == {"prompt", "reference_image_base64"}
    assert base64.b64decode(payload["reference_image_base64"]) == artifact["content"]
    assert artifact["reference_source"] == "upload"
    assert artifact["reference_sha256"] == artifact["sha256"]
    for changed in ({"account_id": "another"}, {"source_id": "another"}, {"channel": "dynamic"}):
        foreign = scope.model_copy(update=changed)
        with pytest.raises(LookupError):
            service.read(foreign, status["artifact_id"])
        with pytest.raises(LookupError):
            service.status(foreign, job["job_id"])


async def test_restart_never_resubmits_uncertain_image(fixture, store):
    service, scope, monitor = fixture
    job = service.decide(scope, request_key="1", context="", draft="A")
    store.execute("UPDATE operation_image_jobs SET status='submitting' WHERE job_id=?", (job["job_id"],))
    service.start()
    try:
        assert service.status(scope, job["job_id"])["status"] == "failed"
        assert not monitor["posts"]
    finally:
        await service.close()


async def test_text_only_does_not_submit(fixture, store, monkeypatch):
    service, scope, monitor = fixture

    async def no_illustration(settings, material):
        return None

    monkeypatch.setattr(ops, "choose", no_illustration)
    job = service.decide(scope, request_key="1", context="", draft="下午见。")
    await service._process(store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (job["job_id"],)))
    assert service.status(scope, job["job_id"])["status"] == "text_only"
    assert not monitor["posts"]


@pytest.mark.parametrize("channel,key", [("comment", "browsing_policy"), ("direct_message", "message_policy"),
                                          ("dynamic", "dynamic_policy")])
async def test_gallery_and_generated_scene_share_scoped_choice(fixture, store, monkeypatch, channel, key):
    images, scope, monitor = fixture
    scope = scope.model_copy(update={"channel": channel})
    if channel == "comment":
        nested = scope.model_copy(update={"source_id": json.dumps([1, "video", "BV1d8pU6xEZX", "42", "11", "13", "124"])})
        with pytest.raises(ValueError, match="仅视频一级评论"):
            images.decide(nested, request_key="nested", context="", draft="楼内文字")
        scope = scope.model_copy(update={"source_id": json.dumps([1, "video", "BV1d8pU6xEZX", "42", "0", "0", ""])})
    images.catalog = MemeCatalog(images.settings, store)
    asset = await images.catalog.register("whale", monitor["selection"].data, title="海边", emotions=["idle"],
                                          source={"kind": "user_upload", "author": "test"},
                                          license={"name": "test"})
    store.set(key, {"use_memes": True, "generate_images": True})
    async def pick(settings, material):
        offer = json.loads(material)
        assert offer["variants"] == ["q", "mature"]
        assert asset["asset_id"] in [item["asset_id"] for item in offer["gallery"]]
        return ops.GalleryImage(asset_id=asset["asset_id"], sha256=asset["sha256"])
    monkeypatch.setattr(ops, "choose", pick)
    job = images.decide(scope, request_key="gallery", context="海边", draft="想去看海。")
    row = store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (job["job_id"],))
    await images._process(row)
    result = images.status(scope, job["job_id"])
    assert (result["status"], result["asset_id"], result["asset_sha256"]) == ("gallery", asset["asset_id"], asset["sha256"])
    assert not monitor["posts"]
    other = images.decide(scope, request_key="changed", context="海边", draft="一起看海。")
    store.set(key, {"use_memes": False, "generate_images": False})
    await images._process(store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (other["job_id"],)))
    assert images.status(scope, other["job_id"])["status"] == "text_only"
    assert not monitor["posts"]


async def test_restart_with_remote_receipt_only_reads_existing_job(fixture, store):
    service, scope, monitor = fixture
    job = service.decide(scope, request_key="resume", context="", draft="看海。")
    brief = ops.ImageBrief(variant="q", prompt="海边").model_dump_json()
    store.execute("UPDATE operation_image_jobs SET status='running',brief=?,endpoint=?,remote_id=? WHERE job_id=?",
                  (brief, "http://gateway/v1", "e" * 32, job["job_id"]))
    await service._process(store.one("SELECT * FROM operation_image_jobs WHERE job_id=?", (job["job_id"],)))
    assert service.status(scope, job["job_id"])["status"] == "ready"
    assert not monitor["posts"]


async def test_gateway_rejection_retries_only_explicit_full_queue(monkeypatch):
    from tuber_studio import workstation_images
    delays = []

    async def wait(delay):
        delays.append(delay)

    def handle(request):
        if not delays:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(202, json={"id": "a" * 32})

    monkeypatch.setattr(workstation_images.asyncio, "sleep", wait)
    async with httpx.AsyncClient(base_url="http://gateway/v1/", transport=httpx.MockTransport(handle)) as client:
        assert await workstation_images.submit(client, {}) == "a" * 32
    assert delays == [3]
