"""Private per-character references for workstation illustration jobs."""

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response
from PIL import Image, ImageOps

from .config import atomic_bytes

Variant = Literal["q", "mature"]
MAX_BYTES = 4 * 1024 * 1024
PREFIX = "/api/settings/artwork-references"
DEFAULTS = {
    "q": ("Q_original.png", "19657099d648fa6ebef207f0b0b091039a53361ddaa0624602d91416c8ca1e31"),
    "mature": ("original.png", "7a76009ff5cf8afabe5d9bdc3136ab7e59ae248c8a8cb9eed6529632e5af35b3"),
}


@dataclass(frozen=True)
class Reference:
    data: bytes
    sha256: str
    source: str


def _selection(settings, character_id, variant):
    if variant not in DEFAULTS:
        raise ValueError("未知的参考图版本")
    role = hashlib.sha256(character_id.encode()).hexdigest()
    return settings.runtime / "private-artifacts" / "artwork-references" / role / variant


def _location(settings, character_id, variant, digest=None):
    folder = _selection(settings, character_id, variant)
    baseline = folder / "default"
    default_digest = baseline.read_text("ascii") if baseline.is_file() else None
    pointer = folder / "active"
    if digest is None:
        digest = pointer.read_text("ascii") if pointer.is_file() else default_digest
    if digest is not None:
        if not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("参考图版本无效")
        upload = folder / digest
        if upload.is_file():
            return upload, digest, "default" if digest == default_digest else "upload"
    name, approved = DEFAULTS[variant]
    if character_id == "whale" and digest in (None, approved):
        default = settings.runtime / "private-artifacts" / "artwork-defaults" / name
        if default.is_file():
            return default, approved, "default"
    raise FileNotFoundError("当前角色的参考图不可用")


def resolve_reference(settings, character_id, variant, *, sha256=None):
    path, expected, source = _location(settings, character_id, variant, sha256)
    with path.open("rb") as file:
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("参考图内容与选定版本不符")
    return Reference(data, expected, source)


def _validate_upload(data):
    try:
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG")) as picture:
            if picture.width * picture.height > 8_000_000 or picture.n_frames != 1:
                raise ValueError("请选择不超过 800 万像素的单张 PNG 或 JPEG")
            picture.verify()
        with Image.open(io.BytesIO(data), formats=("JPEG", "PNG")) as picture:
            picture.load()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError("无法读取完整的 PNG 或 JPEG 参考图") from None


def register_artwork_references(app, settings, store, require_admin):
    route = PREFIX + "/{character_id}"
    auth = [Depends(require_admin)]

    @app.get(route, dependencies=auth)
    def list_references(character_id: str):
        store.character(character_id)
        variants = {}
        for variant in DEFAULTS:
            try:
                _, digest, source = _location(settings, character_id, variant)
                url = f"{PREFIX}/{quote(character_id, safe='')}/{variant}?sha256={digest}"
                variants[variant] = dict(available=True, source=source, sha256=digest, preview_url=url,
                                         thumbnail_url=url + "&thumbnail=true")
            except FileNotFoundError:
                variants[variant] = dict(available=False, source=None, sha256=None, preview_url=None)
        return dict(character_id=character_id, variants=variants)

    @app.get(route + "/{variant}", dependencies=auth)
    def preview(character_id: str, variant: Variant, sha256: str | None = None, thumbnail: bool = False):
        store.character(character_id)
        try:
            ref = resolve_reference(settings, character_id, variant, sha256=sha256)
        except FileNotFoundError:
            raise HTTPException(404, "该版本参考图不可用") from None
        if thumbnail:
            with Image.open(io.BytesIO(ref.data)) as picture:
                picture.thumbnail((224, 224), Image.Resampling.LANCZOS)
                thumb = ImageOps.exif_transpose(picture).convert("RGBA")
                out = io.BytesIO()
                thumb.save(out, "PNG")
            return Response(out.getvalue(), media_type="image/png")
        mime = "image/png" if ref.data.startswith(b"\x89PNG") else "image/jpeg"
        return Response(ref.data, media_type=mime)

    @app.put(route + "/{variant}", dependencies=auth)
    async def replace(character_id: str, variant: Variant, request: Request):
        store.character(character_id)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_BYTES:
                raise HTTPException(413, "参考图不能超过 4 MiB")
            data.extend(chunk)
        _validate_upload(data)
        digest = hashlib.sha256(data).hexdigest()
        folder = _selection(settings, character_id, variant)
        atomic_bytes(folder / digest, data, private=True)
        atomic_bytes(folder / "active", digest.encode("ascii"), private=True)
        return list_references(character_id)

    @app.delete(route + "/{variant}", dependencies=auth)
    def restore(character_id: str, variant: Variant):
        store.character(character_id)
        (_selection(settings, character_id, variant) / "active").unlink(missing_ok=True)
        return list_references(character_id)
