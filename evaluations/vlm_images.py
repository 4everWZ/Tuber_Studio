"""Explicit, reproducible spatial preprocessing for VLM evaluation images.

No files are written. ``server_native`` preserves the supplied bytes; the
Gemma profile follows the pinned official PIL processor's resize calculation.
Pixel normalization remains the model runtime's responsibility.
"""

import hashlib
import io
import math
import struct

PROFILES = ("server_native", "gemma4_image_280")
PILLOW_REQUIREMENT = "pillow==13.0.0"
OFFICIAL_SOURCES = [
    "https://huggingface.co/google/gemma-4-E2B-it/blob/3e22461f65e89153144f8adb70e3b8c2cc9845a7/processor_config.json",
    "https://huggingface.co/google/gemma-4-E4B-it/blob/ee0ef6023621cff504d758262d4e04895a5af4a2/processor_config.json",
    "https://github.com/huggingface/transformers/blob/v5.5.0/src/transformers/models/gemma4/image_processing_pil_gemma4.py",
]


def inspect_image(raw: bytes) -> tuple[str, int, int]:
    """Read bounded PNG/JPEG dimensions without an optional image dependency."""
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Optional image exceeds 8MiB")
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24 and raw[12:16] == b"IHDR":
        width, height = struct.unpack(">II", raw[16:24])
        mime = "image/png"
    elif raw[:2] == b"\xff\xd8":
        offset, width, height = 2, None, None
        while offset < len(raw):
            if raw[offset] != 255:
                raise ValueError("Malformed JPEG marker")
            while offset < len(raw) and raw[offset] == 255:
                offset += 1
            if offset >= len(raw):
                break
            marker = raw[offset]
            offset += 1
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xDA or offset + 2 > len(raw):
                break
            size = int.from_bytes(raw[offset : offset + 2], "big")
            if size < 2 or offset + size > len(raw):
                raise ValueError("Malformed JPEG segment")
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if size < 8:
                    raise ValueError("Malformed JPEG frame")
                height = int.from_bytes(raw[offset + 3 : offset + 5], "big")
                width = int.from_bytes(raw[offset + 5 : offset + 7], "big")
                break
            offset += size
        if width is None or height is None:
            raise ValueError("JPEG has no readable frame dimensions")
        mime = "image/jpeg"
    else:
        raise ValueError("Only actual PNG or JPEG image bytes are accepted")
    if not 0 < width * height <= 16 * 1024 * 1024:
        raise ValueError("Image pixel bounds exceeded")
    return mime, width, height


def gemma4_target_size(width: int, height: int) -> tuple[int, int]:
    """Return (width, height) using Transformers v5.5.0's Apache-2.0 formula.

    The official 280-soft-token image profile uses 16px patches pooled 3x3.
    Aspect-ratio rounding can produce fewer than 280 spatial tokens.
    """
    if type(width) is not int or type(height) is not int or min(width, height) <= 0:
        raise ValueError("Image dimensions must be positive integers")
    max_patches, patch_size, pooling = 280 * 3**2, 16, 3
    target_pixels = max_patches * patch_size**2
    factor = math.sqrt(target_pixels / (height * width))
    multiple = pooling * patch_size
    target_height = math.floor(factor * height / multiple) * multiple
    target_width = math.floor(factor * width / multiple) * multiple
    if target_height == 0 and target_width == 0:
        raise ValueError("Image is too small for Gemma's patch grid")
    maximum_side = (max_patches // pooling**2) * multiple
    if target_height == 0:
        target_height = multiple
        target_width = min(math.floor(width / height) * multiple, maximum_side)
    elif target_width == 0:
        target_width = multiple
        target_height = min(math.floor(height / width) * multiple, maximum_side)
    return target_width, target_height


def prepare_image(data: bytes, mime_type: str, profile: str = "server_native") -> dict:
    """Return data/MIME and provenance metadata; leave persistence to the caller.

    ``metadata.parameters.expected_visual_tokens_from_grid`` is a calculation,
    not a measurement of runtime image tokens. Runtime observations are separate.
    """
    if profile not in PROFILES:
        raise ValueError(f"Unsupported image preprocessing profile: {profile}")
    actual_mime, width, height = inspect_image(data)
    if mime_type != actual_mime:
        raise ValueError("Declared image MIME does not match image bytes")
    digest = hashlib.sha256(data).hexdigest()
    metadata = {
        "profile": profile,
        "parent_sha256": digest,
        "derived_sha256": digest,
        "parent_mime_type": mime_type,
        "derived_mime_type": mime_type,
        "parent_size": {"width": width, "height": height},
        "derived_size": {"width": width, "height": height},
        "parameters": {"operation": "identity"},
        "official_sources": [],
    }
    if profile == "server_native":
        return {"data": data, "mime_type": mime_type, "metadata": metadata}

    try:
        from PIL import Image, __version__
    except ModuleNotFoundError as exc:
        if exc.name != "PIL":
            raise
        raise RuntimeError(
            f"Gemma image preprocessing requires the evaluation extra: "
            f"uv run --locked --with {PILLOW_REQUIREMENT} python ..."
        ) from exc

    target_width, target_height = gemma4_target_size(width, height)
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        if source.size != (width, height):
            raise ValueError("Decoded image dimensions differ from the image header")
        converted = source.mode != "RGB"
        resized = source.size != (target_width, target_height)
        if converted or resized:
            rgb = source.convert("RGB")
            if resized:
                rgb = rgb.resize((target_width, target_height), Image.Resampling.BICUBIC)
            output = io.BytesIO()
            rgb.save(output, format="PNG", compress_level=9)
            data, mime_type = output.getvalue(), "image/png"

    metadata.update(
        derived_sha256=hashlib.sha256(data).hexdigest(),
        derived_mime_type=mime_type,
        derived_size={"width": target_width, "height": target_height},
        official_sources=list(OFFICIAL_SOURCES),
        parameters={
            "operation": "official_spatial_resize_and_rgb",
            "resize_performed": resized,
            "rgb_conversion_performed": converted,
            "max_soft_tokens": 280,
            "patch_size": 16,
            "pooling_kernel_size": 3,
            "expected_visual_tokens_from_grid": (target_width // 48) * (target_height // 48),
            "interpolation": "Pillow.Resampling.BICUBIC",
            "pillow_version": __version__,
            "output_encoding": "lossless_png" if converted or resized else "original_bytes",
            "runtime_pixel_normalization": True,
            "crop": None,
        },
    )
    return {"data": data, "mime_type": mime_type, "metadata": metadata}
