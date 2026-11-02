"""Actual catalog registration, no image model, download or platform requests."""

import asyncio
from pathlib import Path

import pytest

from tuber_studio.meme_generation import GeneratedMemeImage, MemeGenerationRequest, MemeGenerationService
from tuber_studio.memes import MAX_BYTES, MemeCatalog


def request(**options):
    return MemeGenerationRequest.model_validate(
        {
            "character_id": "whale",
            "prompt": "一张大肥鱼开心的角色表情",
            "title": "开心",
            "emotions": ["cheerful"],
            "license": {"name": "fixture only; no real generator invoked"},
        }
        | options
    )


class Generator:
    id, version, timeout_seconds = "controlled-generator", "fixture-v1", 2

    def __init__(self, image):
        self.image = image
        self.calls, self.closed, self.active, self.peak = 0, 0, 0, 0
        self.entered, self.release = asyncio.Event(), None

    async def generate(self, task):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            self.entered.set()
            if self.release:
                await self.release.wait()
            # The generator cannot rewrite the operator's registration license.
            task.license.name = "provider tried to change metadata"
            return self.image
        finally:
            self.active -= 1

    async def close(self):
        self.closed += 1


@pytest.fixture
def catalog(settings, store):
    return MemeCatalog(settings, store)


@pytest.fixture
def image(settings):
    return GeneratedMemeImage(
        content=(settings.root / "assets/whale/cheerful.gif").read_bytes(), mime_type="image/gif"
    )


async def test_unmounted_default_has_no_generation_or_catalog_side_effects(catalog, store):
    service = MemeGenerationService(catalog)
    assert service.describe()["available"] is False
    with pytest.raises(ValueError, match="尚未挂载"):
        await service.generate(request())
    await service.close()
    assert not store.all("SELECT * FROM meme_assets")
    assert not list(catalog.library.iterdir())


async def test_generated_original_is_validated_and_registered_disabled(catalog, image, store):
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    generator = Generator(image)
    service = MemeGenerationService(catalog, generator)
    task = request()
    asset = await service.generate(task)
    assert asset["animated"] and asset["source"]["kind"] == "generated"
    assert asset["source"]["author"] == generator.id and asset["source"]["revision"] == generator.version
    assert task.prompt not in asset["source"]["note"]
    assert asset["license"]["name"] == task.license.name
    assert not asset["enabled"] and catalog.list("whale") == []
    assert catalog.read(asset["asset_id"], "whale", allow_disabled=True)["content"] == image.content
    with pytest.raises(ValueError, match="未启用"):
        catalog.select({"asset_id": asset["asset_id"]}, "whale")
    catalog.enable(asset["asset_id"])
    assert catalog.select({"asset_id": asset["asset_id"]}, "whale")["asset_id"] == asset["asset_id"]
    await service.close()
    await service.close()
    assert generator.closed == 1 and not service.describe()["available"]


@pytest.mark.parametrize(
    "output", ["https://invalid.test/a.png", Path("a.png"), {"url": "https://invalid.test/a.png"}]
)
async def test_locator_result_is_never_fetched_opened_or_registered(catalog, output, store):
    with pytest.raises(ValueError, match="字节结果"):
        await MemeGenerationService(catalog, Generator(output)).generate(request())
    assert not store.all("SELECT * FROM meme_assets") and not list(catalog.library.iterdir())


@pytest.mark.parametrize("content", [b"not an image", b"GIF89a" + b"broken"])
async def test_invalid_generated_bytes_cannot_bypass_native_image_validation(catalog, content, store):
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    with pytest.raises(ValueError):
        await MemeGenerationService(
            catalog, Generator(GeneratedMemeImage(content=content, mime_type="image/gif"))
        ).generate(request())
    assert not store.all("SELECT * FROM meme_assets") and not list(catalog.library.iterdir())


@pytest.mark.parametrize("content", ["https://invalid.test/a.gif", b"", b"x" * (MAX_BYTES + 1)])
def test_result_requires_bounded_raw_bytes(content):
    with pytest.raises(ValueError):
        GeneratedMemeImage(content=content, mime_type="image/gif")


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
async def test_cancellation_and_timeout_clean_up_before_registration(catalog, image, store, mode):
    generator = Generator(image)
    generator.release = asyncio.Event()
    if mode == "timeout":
        generator.timeout_seconds = 0.02
    service = MemeGenerationService(catalog, generator)
    pending = asyncio.create_task(service.generate(request()))
    await asyncio.wait_for(generator.entered.wait(), 1)
    if mode == "cancel":
        pending.cancel()
    with pytest.raises(asyncio.CancelledError if mode == "cancel" else TimeoutError):
        await pending
    assert generator.active == 0 and not store.all("SELECT * FROM meme_assets")
    assert not list(catalog.library.iterdir())


async def test_unknown_role_is_rejected_before_inference(catalog, image):
    generator = Generator(image)
    with pytest.raises(ValueError):
        await MemeGenerationService(catalog, generator).generate(request(character_id="not_an_oc"))
    assert generator.calls == 0


async def test_concurrent_callers_share_one_bounded_generation_slot(catalog, image):
    if not catalog.inspector.binary:
        pytest.skip("真实ffprobe未安装")
    generator = Generator(image)
    generator.release = asyncio.Event()
    service = MemeGenerationService(catalog, generator)
    first = asyncio.create_task(service.generate(request()))
    await generator.entered.wait()
    second = asyncio.create_task(service.generate(request(title="second")))
    await asyncio.sleep(0)
    generator.release.set()
    results = await asyncio.gather(first, second)
    assert len(results) == generator.calls == 2 and generator.peak == 1


async def test_interrupted_close_can_still_release_generator_resources(catalog, image):
    generator = Generator(image)
    generator.release = asyncio.Event()
    service = MemeGenerationService(catalog, generator)
    generation = asyncio.create_task(service.generate(request()))
    await generator.entered.wait()
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    generation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await generation
    await service.close()
    assert generator.closed == 1 and not service.describe()["available"]
