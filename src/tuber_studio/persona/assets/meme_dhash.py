"""Perceptual difference hash (dHash) for still and animated meme deduplication."""

from __future__ import annotations

import re
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_FRAMES = 300
_HEX_64 = re.compile(r"^[0-9a-f]{16}$")


def _to_grayscale_9x8(frame: Image.Image) -> bytes:
    if frame.mode == "P" and "transparency" in frame.info:
        frame = frame.convert("RGBA")
    if frame.mode in ("RGBA", "LA", "PA"):
        rgba = frame.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        gray = background.convert("L")
    else:
        gray = frame.convert("L")
    resized = gray.resize((9, 8), Image.Resampling.LANCZOS)
    return resized.tobytes()


def _frame_dhash(frame: Image.Image) -> str:
    pixels = _to_grayscale_9x8(frame)
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bits = (bits << 1) | int(pixels[offset + col] > pixels[offset + col + 1])
    return f"{bits:016x}"


def compute_dhash(content: bytes) -> str:
    """Compute a 64-bit dHash (or 'first:middle' pair for animated images)."""
    if not isinstance(content, bytes) or not content:
        raise ValueError("计算 dHash 需要非空图片字节")
    try:
        with Image.open(BytesIO(content)) as image:
            image.seek(0)
            first = _frame_dhash(image)
            raw_frames = int(getattr(image, "n_frames", 1) or 1)
            bounded_frames = min(raw_frames, MAX_FRAMES)
            if bounded_frames > 1:
                image.seek(bounded_frames // 2)
                middle = _frame_dhash(image)
                return f"{first}:{middle}"
            return first
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, EOFError, SyntaxError) as error:
        raise ValueError(f"无法解码图片以计算 dHash: {error}") from error


def _parse_dhash(value: str) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    parts = normalized.split(":")
    if len(parts) not in (1, 2) or any(not _HEX_64.fullmatch(part) for part in parts):
        return None
    return tuple(int(part, 16) for part in parts)


def dhash_hamming(a: str, b: str) -> int:
    """Return Hamming distance between two valid dHash strings."""
    pa = _parse_dhash(a)
    pb = _parse_dhash(b)
    if pa is None or pb is None:
        raise ValueError("dHash 为空或格式无效")
    if len(pa) == 2 and len(pb) == 2:
        return (pa[0] ^ pb[0]).bit_count() + (pa[1] ^ pb[1]).bit_count()
    return (pa[0] ^ pb[0]).bit_count()


def dhash_matches(
    a: str,
    b: str,
    *,
    still_threshold: int = 6,
    animated_threshold: int = 12,
) -> bool:
    """Compare still or animated dHashes within threshold; empty/invalid returns False."""
    pa = _parse_dhash(a)
    pb = _parse_dhash(b)
    if pa is None or pb is None:
        return False
    if len(pa) == 1 and len(pb) == 1:
        return (pa[0] ^ pb[0]).bit_count() <= still_threshold
    if len(pa) == 2 and len(pb) == 2:
        first_dist = (pa[0] ^ pb[0]).bit_count()
        mid_dist = (pa[1] ^ pb[1]).bit_count()
        return (first_dist + mid_dist) <= animated_threshold or (
            first_dist <= still_threshold and mid_dist <= still_threshold
        )
    return (pa[0] ^ pb[0]).bit_count() <= still_threshold
