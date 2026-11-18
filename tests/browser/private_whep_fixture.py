"""Use the upstream Avatar control plane, source tap, media clock and aiortc tracks."""
import os
from pathlib import Path

from aiohttp import web
from services.avatar import app
from services.avatar.renderers import SpriteRenderer


def avatar_app():
    if root := os.environ.get("STUDIO_TEST_RIG_DIR"):
        return app.create_app("internal-fixture", registry={
            key: {"driver": "inochi2d", "root": Path(root) / key / "model.inp"} for key in ("regular", "q")})
    ids = {"portrait_" + n * 32 for n in "ab"}

    class Portrait:
        async def forward(self, request):
            identity = request.match_info["id"]
            if identity not in ids:
                raise web.HTTPNotFound()
            return web.json_response({"id": identity, "state": "ready", "driver": "ditto"})

        async def renderer(self, owner, identity):
            assert identity in ids
            renderer = SpriteRenderer(Path(app.__file__).parent / "assets/ava")
            renderer.capabilities = renderer.capabilities | {"driver": "ditto"}
            return renderer

    upstream = app.create_app("internal-fixture")
    upstream["service"].portrait = Portrait()
    return upstream
