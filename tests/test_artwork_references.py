import hashlib
import io

from fastapi.testclient import TestClient
from PIL import Image

from tuber_studio.app import create_app
from tuber_studio.artwork_references import DEFAULTS, MAX_BYTES, PREFIX, resolve_reference


def test_owner_can_replace_preview_and_restore_without_changing_existing_job(settings, monkeypatch):
    default = (settings.root / "assets/whale/sing_open.png").read_bytes()
    folder = settings.runtime / "private-artifacts/artwork-defaults"
    folder.mkdir(parents=True)
    (folder / "Q_original.png").write_bytes(default)
    monkeypatch.setitem(DEFAULTS, "q", ("Q_original.png", hashlib.sha256(default).hexdigest()))
    # A valid image above the ordinary JSON middleware limit exercises the upload route.
    buf = io.BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(buf, "PNG", compress_level=0)
    upload = buf.getvalue()
    assert 2_100_000 < len(upload) < MAX_BYTES
    app = create_app(settings)
    with TestClient(app) as client:
        path = f"{PREFIX}/whale/q"
        assert client.get(path).status_code == 401
        assert client.put(path, content=upload).status_code == 401
        client.post("/api/auth", json={"token": settings.admin_token}).raise_for_status()
        app.state.store.save_character(app.state.store.character() | {"id": "another", "name": "另一个角色"})
        assert client.get(path).content == default
        assert client.get(f"{PREFIX}/another/q").status_code == 404
        selected = client.put(path, content=upload).json()["variants"]["q"]
        assert selected["source"] == "upload"
        assert client.get(selected["preview_url"]).content == upload
        assert client.get(f"{PREFIX}/whale/mature").status_code == 404
        assert client.get(f"{PREFIX}/another/q", params={"sha256": selected["sha256"]}).status_code == 404
        assert client.put(path, content=b"not an image").status_code == 400
        assert client.put(path, content=b"x" * (MAX_BYTES + 1)).status_code == 413
        assert resolve_reference(settings, "whale", "q").data == upload
        assert client.delete(path).json()["variants"]["q"]["source"] == "default"
        assert resolve_reference(settings, "whale", "q").data == default
        assert resolve_reference(settings, "whale", "q", sha256=selected["sha256"]).data == upload
