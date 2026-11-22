"""Route and static bundle serving tests for `web_dist` entries and `WebMount`."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tuber_studio.app import create_app
from tuber_studio.app import factory as factory_module

ASSET_REF_RE = re.compile(r'(?:src|href)="(static/bundle/[^"]+)"')


def _write_synthetic_web_dist(root: Path) -> None:
    (root / "bundle").mkdir(parents=True)
    (root / "worklets").mkdir(parents=True)
    (root / "studio-path.js").write_text("globalThis.StudioPath = (v) => v;\n", encoding="utf-8")
    (root / "worklets" / "private-call-capture.js").write_text("// worklet\n", encoding="utf-8")
    (root / "bundle" / "workbench-test.js").write_text("console.log('ok');\n", encoding="utf-8")
    (root / "bundle" / "workbench-test.css").write_text("body { margin: 0; }\n", encoding="utf-8")
    (root / "bundle" / "live-test.js").write_text("console.log('live');\n", encoding="utf-8")
    (root / "bundle" / "overlay-test.js").write_text("console.log('overlay');\n", encoding="utf-8")
    (root / "bundle" / "draw-guess-test.js").write_text("console.log('draw-guess');\n", encoding="utf-8")

    entries = {
        "index.html": (
            "<!doctype html><html><head><title>大肥鱼工坊</title>"
            '<script type="module" src="static/bundle/workbench-test.js"></script>'
            '<link rel="stylesheet" href="static/bundle/workbench-test.css">'
            '</head><body><div id="root"></div></body></html>'
        ),
        "live.html": (
            "<!doctype html><html><head><title>直播工作台</title>"
            '<script type="module" src="static/bundle/live-test.js"></script>'
            '</head><body><div id="root"></div></body></html>'
        ),
        "overlay.html": (
            "<!doctype html><html><head><title>Overlay</title>"
            '<script type="module" src="static/bundle/overlay-test.js"></script>'
            '</head><body><div id="root"></div></body></html>'
        ),
        "draw-guess-overlay.html": (
            "<!doctype html><html><head><title>你画我猜</title>"
            '<script type="module" src="static/bundle/draw-guess-test.js"></script>'
            '</head><body><div id="root"></div></body></html>'
        ),
    }
    for name, html in entries.items():
        (root / name).write_text(html, encoding="utf-8")


def test_missing_web_dist_keeps_create_app_and_api_working_with_503_entries(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(factory_module, "_default_web_dist", lambda: tmp_path / "missing_web_dist")

    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/api/v1/system/status").status_code == 401
        headers = {"Authorization": "Bearer " + settings.admin_token}
        assert client.get("/api/v1/system/status", headers=headers).status_code == 200

        for path in ("/", "/live", "/overlay", "/overlay/draw-guess"):
            response = client.get(path)
            assert response.status_code == 503
            assert "pnpm --dir frontend build" in response.text


def test_synthetic_web_dist_serves_entries_and_bundle_assets_at_root_and_prefix(settings, tmp_path, monkeypatch):
    dist = tmp_path / "web_dist"
    _write_synthetic_web_dist(dist)
    monkeypatch.setattr(factory_module, "_default_web_dist", lambda: dist)
    monkeypatch.setenv("TUBER_WEB_PREFIX", "/dafeiyu")

    with TestClient(create_app(settings), base_url="https://studio.example") as client:
        assert client.get("/dafeiyu/static/studio-path.js").status_code == 200
        assert client.get("/static/studio-path.js").status_code == 200
        assert client.get("/dafeiyu/static/worklets/private-call-capture.js").status_code == 200
        assert client.get("/dafeiyu/settings", follow_redirects=False).headers["location"] == "/dafeiyu/#settings"
        assert client.get("/settings", follow_redirects=False).headers["location"] == "/#settings"

        for route in ("/", "/live", "/overlay", "/overlay/draw-guess"):
            root_page = client.get(route)
            assert root_page.status_code == 200
            assert root_page.text.startswith("<!doctype html>")
            assert '<head><base href="/"><script src="static/studio-path.js" defer></script>' in root_page.text

            prefixed_path = "/dafeiyu/" if route == "/" else f"/dafeiyu{route}"
            mounted_page = client.get(prefixed_path)
            assert mounted_page.status_code == 200
            assert '<head><base href="/dafeiyu/"><script src="static/studio-path.js" defer></script>' in mounted_page.text

            refs = ASSET_REF_RE.findall(mounted_page.text)
            assert refs, f"expected bundle asset refs on {route}"
            for ref in refs:
                assert client.get(f"/{ref}").status_code == 200
                assert client.get(f"/dafeiyu/{ref}").status_code == 200


def test_built_web_dist_serves_all_entries_and_bundle_assets(settings, monkeypatch):
    real_dist = Path(factory_module.__file__).resolve().parents[1] / "web_dist"
    if not real_dist.is_dir():
        pytest.skip("web_dist has not been built")

    monkeypatch.setenv("TUBER_WEB_PREFIX", "/dafeiyu")
    with TestClient(create_app(settings), base_url="https://studio.example") as client:
        assert client.get("/dafeiyu/static/studio-path.js").status_code == 200
        assert client.get("/dafeiyu/static/worklets/private-call-capture.js").status_code == 200

        for route in ("/", "/live", "/overlay", "/overlay/draw-guess"):
            root_res = client.get(route)
            assert root_res.status_code == 200
            assert '<base href="/">' in root_res.text

            prefixed = "/dafeiyu/" if route == "/" else f"/dafeiyu{route}"
            mounted_res = client.get(prefixed)
            assert mounted_res.status_code == 200
            assert '<head><base href="/dafeiyu/"><script src="static/studio-path.js" defer></script>' in mounted_res.text

            refs = ASSET_REF_RE.findall(mounted_res.text)
            assert refs, f"expected built bundle script/link references in {route}"
            for ref in refs:
                assert client.get(f"/{ref}").status_code == 200
                assert client.get(f"/dafeiyu/{ref}").status_code == 200
