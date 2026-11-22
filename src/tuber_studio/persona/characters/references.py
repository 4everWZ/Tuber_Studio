"""Private per-character references for workstation illustration jobs."""

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Literal

from PIL import Image

Variant = Literal["q", "mature"]
MAX_BYTES = 4 * 1024 * 1024
PREFIX = "/api/v1/persona/artwork-references"
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

