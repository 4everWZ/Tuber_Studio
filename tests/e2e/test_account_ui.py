"""Verify built React workbench entry shell and static bundle assets."""

import re

from fastapi.testclient import TestClient

from tuber_studio.app import create_app

BUNDLE_REF_RE = re.compile(r'(?:src|href)="(static/bundle/[^"]+)"')


def test_workbench_serves_built_spa_shell_and_assets(settings):
    with TestClient(create_app(settings)) as client:
        page = client.get("/").raise_for_status().text
        assert "<!-- page:" not in page
        assert '<base href="/">' in page
        assert '<script src="static/studio-path.js" defer></script>' in page
        assert '<div id="root"></div>' in page
        assert client.get("/static/studio-path.js").status_code == 200
        refs = BUNDLE_REF_RE.findall(page)
        assert refs
        for ref in refs:
            assert client.get(f"/{ref}").status_code == 200
