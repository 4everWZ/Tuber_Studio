"""Use the upstream Avatar control plane, source tap, media clock and aiortc tracks."""
from pathlib import Path

from aiohttp import web
from services.avatar import app
from services.avatar.renderers import SpriteRenderer


def avatar_app():
    ids = {"portrait_" + n * 32 for n in "ab"}

    class Portrait:
        async def forward(self, request):
            identity = request.match_info["id"]
            if identity not in ids:
                raise web.HTTPNotFound()
            return web.json_response({"id": identity, "state": "ready", "driver": "ditto"})

        async def renderer(self, owner, identity):
            assert identity in ids
            return SpriteRenderer(Path(app.__file__).parent / "assets/ava")

    upstream = app.create_app("internal-fixture")
    upstream["service"].portrait = Portrait()
    return upstream
