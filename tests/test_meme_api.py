import base64

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.memes import MAX_BYTES


class OfflinePlatform:
    async def close(self):
        pass


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, OfflinePlatform())) as value:
        yield value


def login(client, settings):
    client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()


def test_catalog_requires_admin_and_exposes_shared_reaction_pack(client, settings):
    assert client.get("/api/memes").status_code == 401
    login(client, settings)
    listing = client.get("/api/memes").json()
    assert listing["max_file_bytes"] == MAX_BYTES
    assert listing["character_id"] == "whale"
    assert len(listing["items"]) == 23
    archive = [item for item in listing["items"] if item["source"]["kind"] == "licensed_pack"]
    assert {item["title"] for item in archive} == {"花花", "被窝", "鲸鱼思考"}
    for item in archive:
        assert item["enabled"] and item["source"]["author"] == "1cyberlangke1"
        assert f'/ai-girl-stickers/blob/{item["source"]["revision"]}/dist/submissions/originals/' in item["source"]["url"]
        assert item["license"]["name"] == "CC0 1.0"
        assert item["license"]["url"] == "https://github.com/lmy414/ai-girl-stickers/issues/32"
        preview = client.get(f"/api/memes/{item['asset_id']}/preview")
        assert preview.headers["content-type"] == "image/webp" and preview.content[8:12] == b"WEBP"
    assert sum(item["enabled"] and item["mime_type"] == "image/png" for item in listing["items"]) == 8
    new_clips = [item for item in listing["items"] if item["enabled"] and item["animated"]]
    assert {item["title"] for item in new_clips} == {"摇摆一下", "开饭啦", "蹦起来", "困了，晚安", "悄悄偷吃", "这就来"}
    for clip in new_clips:
        preview = client.get(f"/api/memes/{clip['asset_id']}/preview")
        assert preview.status_code == 200 and preview.headers["content-type"] == "image/gif"
    asset = next(item for item in listing["items"] if item["animated"] and not item["enabled"])
    assert not asset["enabled"]
    assert "path" not in asset
    identity = asset["asset_id"]
    preview = client.get(f"/api/memes/{identity}/preview", params={"character": "whale"})
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/gif"
    assert preview.content.startswith((b"GIF87a", b"GIF89a"))
    enabled = client.put(f"/api/memes/{identity}", json={"character": "whale", "enabled": True})
    assert enabled.status_code == 200 and enabled.json()["enabled"]
    assert client.get("/api/memes?character=not_a_role").status_code == 400
    assert client.get("/api/memes/http%3A%2F%2Fevil.example/preview?character=whale").status_code == 404


def test_json_upload_preserves_original_and_delete_never_removes_project_source(client, settings):
    login(client, settings)
    original = settings.root / "assets/whale/cheerful.gif"
    content = original.read_bytes()
    body = {
        "character": "whale",
        "title": "测试开心表情",
        "emotions": ["cheerful"],
        "source": {"kind": "user_upload", "author": "测试素材所有者"},
        "license": {"name": "本地测试"},
        "content_base64": base64.b64encode(content).decode(),
        "import_id": "8453bc82-62b1-43f2-967a-0b014b16956b",
        "tags": ["跳舞", " Q版 ", "ｑ版"],
        "description": "大肥鱼的开心动作。",
    }
    response = client.post("/api/memes", json=body)
    assert response.status_code == 200, response.text
    asset = response.json()
    identity = asset["asset_id"]
    assert asset["tags"] == ["跳舞", "Q版"]
    assert client.post("/api/memes", json=body).json()["asset_id"] == identity
    labels_url = f"/api/memes/{identity}/labels"
    assert client.put(labels_url, json={"tags": ["  "]}).status_code == 422
    edited = client.put(labels_url, json={"tags": ["庆祝"], "description": "庆祝时的全身动作。"}).json()
    assert edited["sha256"] == asset["sha256"]
    assert edited["source"] == asset["source"] and edited["license"] == asset["license"]
    assert client.post("/api/memes", json=body).json()["tags"] == ["庆祝"]
    assert asset["animated"] and asset["storage_kind"] == "library"
    assert client.get(f"/api/memes/{identity}/preview?character=whale").content == content
    for category in ("music", "speech", "temporary", "browsing"):
        client.post(f"/api/cache/{category}/clear").raise_for_status()
    assert client.get(f"/api/memes/{identity}/preview?character=whale").content == content
    assert client.delete(f"/api/memes/{identity}?character=whale").status_code == 400
    client.put(f"/api/memes/{identity}", json={"character": "whale", "enabled": False}).raise_for_status()
    assert client.delete(f"/api/memes/{identity}?character=whale").json()["deleted"]
    assert original.read_bytes() == content
    assert not list((settings.runtime / "memes/library").glob("*.gif"))
    assert client.get(f"/api/memes/{identity}/preview?character=whale").status_code == 400
    assert (
        client.post("/api/memes", json=body | {"content_base64": "data:image/gif;base64,AAAA"}).status_code
        == 400
    )
    assert (
        client.post("/api/memes", json=body | {"image_url": "https://evil.example/a.png"}).status_code == 422
    )
    assert (
        client.post(
            "/api/memes", json=body | {"content_base64": base64.b64encode(b"not an image").decode()}
        ).status_code
        == 400
    )


def test_catalog_originals_are_not_evicted_by_media_cache(client, settings):
    login(client, settings)
    items = client.get("/api/memes").json()["items"]
    identity = items[0]["asset_id"]
    # Catalog seeds refer to immutable repository assets rather than cache copies.
    assert items[0]["storage_kind"] == "existing_user_assets"
    assert not list((settings.runtime / "memes/library").iterdir())
    for category in ("music", "speech", "temporary", "browsing"):
        client.post(f"/api/cache/{category}/clear").raise_for_status()
    assert client.get(f"/api/memes/{identity}/preview?character=whale").status_code == 200
    assert (
        client.post("/api/memes", content=b"x", headers={"Content-Length": str(20 * 1024 * 1024)}).status_code
        == 413
    )
