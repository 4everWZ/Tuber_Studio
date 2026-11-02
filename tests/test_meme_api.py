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


def test_catalog_requires_admin_and_binds_actual_role_state(client, settings):
    assert client.get("/api/memes").status_code == 401
    login(client, settings)
    listing = client.get("/api/memes").json()
    assert listing["max_file_bytes"] == MAX_BYTES
    assert listing["character_id"] == "whale"
    assert len(listing["items"]) == 6
    asset = listing["items"][0]
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
    }
    response = client.post("/api/memes", json=body)
    assert response.status_code == 200, response.text
    asset = response.json()
    identity = asset["asset_id"]
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
