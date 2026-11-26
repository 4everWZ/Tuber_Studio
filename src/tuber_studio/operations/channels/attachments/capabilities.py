"""Attachment capability for a Bilibili comment destination."""

from ....infra.platforms.bilibili.account import account_comment_capabilities

ANIMATED_MIMES = frozenset({"image/gif"})


def video_top_level_images(resource, root, parent):
    return resource in ("video", 1) and str(root) == str(parent) == "0"


def _is_animated_webp(data):
    return len(data) >= 16 and data.startswith(b"RIFF") and data[8:12] == b"WEBP" and b"ANIM" in data[:128]


def is_animated_image(image_or_asset):
    """Return True if an image payload or meme catalog row is animated (GIF / animated WebP)."""
    if not isinstance(image_or_asset, dict):
        return False
    mime = str(image_or_asset.get("mime_type") or "").strip().lower()
    if mime in ANIMATED_MIMES or image_or_asset.get("animated") is True:
        return True
    top_frames = image_or_asset.get("frames")
    if type(top_frames) is int and top_frames > 1:
        return True
    for nested_key in ("image_info", "asset"):
        nested = image_or_asset.get(nested_key)
        if isinstance(nested, dict) and is_animated_image(nested):
            return True
    blob_name = str(image_or_asset.get("blob_name") or "").strip().lower()
    if blob_name.endswith(".gif"):
        return True
    if blob_name and "." not in blob_name and not image_or_asset.get("image_info") and not mime:
        # Bundled whale preset GIFs store bare names like 'cheerful' without extension.
        return True
    content = image_or_asset.get("content")
    if isinstance(content, (bytes, bytearray)):
        raw = bytes(content[:128])
        if raw.startswith((b"GIF87a", b"GIF89a")) or _is_animated_webp(raw):
            return True
    return False


def comment_attachment_allowed(store, account_id, image_or_asset):
    """Verify whether `account_id` may send `image_or_asset` in a Bilibili comment."""
    if image_or_asset is None:
        return True, None
    caps = account_comment_capabilities(store, account_id)
    if not caps["comment_allowed"]:
        return False, caps["reason"] or "当前 B 站账号等级不足，暂无法发送评论"
    if not caps["comment_image_allowed"]:
        return False, caps["reason"] or "当前账号暂无评论配图发送权限"
    if is_animated_image(image_or_asset) and not caps["comment_gif_allowed"]:
        return False, caps["reason"] or "当前账号无评论动图（GIF）发送权限，请改用静态图或纯文字"
    return True, None
