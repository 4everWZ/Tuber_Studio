"""Image adaptation provenance and the official Gemma spatial grid."""

import builtins
import hashlib
import importlib.util
import io
import struct
import zlib
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "vlm_images", Path(__file__).resolve().parents[1] / "evaluations" / "vlm_images.py"
)
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)


def png(width=480, height=270):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * width) * height))
        + chunk(b"IEND", b"")
    )


def block_pillow(monkeypatch):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("blocked optional dependency", name="PIL")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)


def test_native_preserves_bytes_and_requires_no_pillow(monkeypatch):
    block_pillow(monkeypatch)
    original = png()
    result = images.prepare_image(original, "image/png")
    assert result["data"] is original
    assert result["mime_type"] == "image/png"
    metadata = result["metadata"]
    assert metadata["parent_sha256"] == metadata["derived_sha256"] == hashlib.sha256(original).hexdigest()
    assert metadata["parent_size"] == metadata["derived_size"] == {"width": 480, "height": 270}
    assert metadata["parameters"] == {"operation": "identity"}


@pytest.mark.parametrize(
    "data,error",
    [
        (b"not an image", "actual PNG or JPEG"),
        (b"\xff\xd8\xff\xe1\x00\x01", "Malformed JPEG segment"),
        (b"\xff\xd8", "no readable frame"),
        (b"\x00" * (8 * 1024 * 1024 + 1), "exceeds 8MiB"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", 0, 200), "pixel bounds"),
    ],
)
def test_rejects_invalid_image_headers(data, error):
    with pytest.raises(ValueError, match=error):
        images.inspect_image(data)


def test_jpeg_frame_dimensions_without_decode():
    frame = b"\xff\xd8\xff\xc0\x00\x08\x08" + struct.pack(">HH", 270, 480) + b"\x00"
    assert images.inspect_image(frame) == ("image/jpeg", 480, 270)


def test_bad_mime_or_profile_is_explicit():
    with pytest.raises(ValueError, match="MIME"):
        images.prepare_image(png(), "image/jpeg")
    with pytest.raises(ValueError, match="Unsupported"):
        images.prepare_image(png(), "image/png", "unknown")


@pytest.mark.parametrize(
    "size,expected",
    [
        ((480, 270), (1056, 576)),
        ((270, 480), (576, 1056)),
        ((512, 512), (768, 768)),
        ((10000, 1), (13440, 48)),
        ((1, 10000), (48, 13440)),
    ],
)
def test_official_280_token_spatial_sizes(size, expected):
    assert images.gemma4_target_size(*size) == expected
    assert expected[0] * expected[1] <= 280 * 48**2


def test_gemma_missing_extra_does_not_silently_use_native(monkeypatch):
    block_pillow(monkeypatch)
    with pytest.raises(RuntimeError, match="pillow==13.0.0"):
        images.prepare_image(png(), "image/png", "gemma4_image_280")


def test_gemma_resize_preserves_spatial_content_and_records_real_bytes():
    Image = pytest.importorskip("PIL.Image")
    original = Image.new("RGB", (480, 270), "red")
    original.paste("blue", (240, 0, 480, 270))
    buffer = io.BytesIO()
    original.save(buffer, format="PNG")
    raw = buffer.getvalue()
    result = images.prepare_image(raw, "image/png", "gemma4_image_280")
    assert result == images.prepare_image(raw, "image/png", "gemma4_image_280")
    with Image.open(io.BytesIO(result["data"])) as derived:
        assert derived.size == (1056, 576)
        assert derived.mode == "RGB"
        assert derived.getpixel((0, 0)) == (255, 0, 0)
        assert derived.getpixel((1055, 575)) == (0, 0, 255)
    metadata = result["metadata"]
    assert metadata["parent_sha256"] == hashlib.sha256(raw).hexdigest()
    assert metadata["derived_sha256"] == hashlib.sha256(result["data"]).hexdigest()
    assert metadata["derived_sha256"] != metadata["parent_sha256"]
    assert metadata["parameters"]["expected_visual_tokens_from_grid"] == 264
    assert metadata["parameters"]["crop"] is None
    assert metadata["official_sources"] == images.OFFICIAL_SOURCES


def test_gemma_rgb_conversion_and_already_sized_passthrough():
    Image = pytest.importorskip("PIL.Image")
    grayscale = Image.new("L", (768, 768), 128)
    output = io.BytesIO()
    grayscale.save(output, format="PNG")
    converted = images.prepare_image(output.getvalue(), "image/png", "gemma4_image_280")
    with Image.open(io.BytesIO(converted["data"])) as derived:
        assert derived.mode == "RGB"
        assert derived.getpixel((0, 0)) == (128, 128, 128)
    rgb = Image.new("RGB", (1056, 576), "red")
    output = io.BytesIO()
    rgb.save(output, format="JPEG")
    raw = output.getvalue()
    unchanged = images.prepare_image(raw, "image/jpeg", "gemma4_image_280")
    assert unchanged["data"] is raw
    assert unchanged["mime_type"] == "image/jpeg"
    assert unchanged["metadata"]["parameters"]["resize_performed"] is False
