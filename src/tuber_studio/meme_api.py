"""Administrator-only catalog management; original images never become cache."""

import base64
import binascii

from fastapi import Depends, Response
from pydantic import Field

from .memes import MAX_BYTES, MemeLicense, MemeSource
from .models import StrictModel

MAX_REQUEST = ((MAX_BYTES + 2) // 3) * 4 + 65536


class MemeUpload(StrictModel):
    character: str | None = Field(default=None, min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=120)
    emotions: list[str] = Field(min_length=1, max_length=20)
    source: MemeSource
    license: MemeLicense
    content_base64: str = Field(min_length=4, max_length=((MAX_BYTES + 2) // 3) * 4)


class MemeAvailability(StrictModel):
    character: str | None = Field(default=None, min_length=1, max_length=50)
    enabled: bool


def register_meme_api(app, catalog, runtime, require_admin):
    auth = [Depends(require_admin)]

    @app.get("/api/memes", dependencies=auth)
    async def listing(character: str | None = None):
        await catalog.seed_existing()
        identity = character or runtime.character["id"]
        items = catalog.list(identity, include_disabled=True)
        return {
            "items": items,
            "character_id": identity,
            "max_file_bytes": MAX_BYTES,
        }

    @app.post("/api/memes", dependencies=auth)
    async def upload(body: MemeUpload):
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("图片base64无效") from None
        return await catalog.register(
            body.character or runtime.character["id"],
            content,
            title=body.title,
            emotions=body.emotions,
            source=body.source.model_dump(),
            license=body.license.model_dump(),
        )

    @app.get("/api/memes/{asset_id}/preview", dependencies=auth)
    async def preview(asset_id: str, character: str | None = None):
        result = catalog.read(asset_id, character or runtime.character["id"], allow_disabled=True)
        return Response(result["content"], media_type=result["mime_type"])

    @app.put("/api/memes/{asset_id}", dependencies=auth)
    async def availability(asset_id: str, body: MemeAvailability):
        return catalog.enable(asset_id, body.enabled)

    @app.delete("/api/memes/{asset_id}", dependencies=auth)
    async def delete(asset_id: str):
        return catalog.delete(asset_id)
