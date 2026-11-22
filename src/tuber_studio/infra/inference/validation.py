"""Validation of untrusted media payloads entering model inference endpoints."""


def validate_image(image, mime_type):
    if not isinstance(image, bytes) or not 0 < len(image) <= 8_000_000:
        raise ValueError("图片须为非空字节，最大8MB")
    valid = (
        mime_type == "image/png"
        and image.startswith(b"\x89PNG\r\n\x1a\n")
        or mime_type == "image/jpeg"
        and image.startswith(b"\xff\xd8\xff")
        or mime_type == "image/webp"
        and image[:4] == b"RIFF"
        and image[8:12] == b"WEBP"
    )
    if not valid:
        raise ValueError("仅接受与MIME匹配的PNG、JPEG或WebP字节；不接受URL或系统文件路径")
