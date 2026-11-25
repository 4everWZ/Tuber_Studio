"""Bounded public evidence from attachments on a verified source comment."""

import asyncio
import re
import time

import httpx

from ....core.models import content_hash
from ....infra.platforms.bilibili.comment_materials import (
    MAX_ATTACHMENTS,
    MAX_COMMENT_MATERIAL_SECONDS,
    CommentMaterialError,
    comment_image_url,
    sample_comment_image,
)


async def prepare_comment_materials(browsing, target, source_row, *, row=None):
    """Keep provider output and unavailable diagnostics tied to the current source."""
    descriptors = source_row.get("attachments")
    if not isinstance(descriptors, list) or not descriptors:
        return []
    adapter = browsing.adapter
    provider = browsing.visual_provider
    capability = provider.capabilities() if provider and hasattr(provider, "analyze_comment_images") else {}
    task = {"id": "comment-material:" + str(target["parent"])}
    if row and row.get("id"):
        task["round_id"] = row["id"]
    deadline = min(time.time() + MAX_COMMENT_MATERIAL_SECONDS, (row or {}).get("deadline") or float("inf"))
    observations = []
    downloaded_images = {}
    for descriptor in descriptors[:MAX_ATTACHMENTS]:
        if not isinstance(descriptor, dict):
            continue
        kind = descriptor.get("kind")
        identity = descriptor.get("article_id") if kind == "article" else comment_image_url(descriptor.get("url"))
        if (kind == "article" and not re.fullmatch(r"[1-9][0-9]{0,19}", str(identity))) or (
            kind == "image" and identity is None
        ) or kind not in {"article", "image"}:
            continue
        identity = str(identity)
        source = {"kind": kind, "source": f"https://www.bilibili.com/read/cv{identity}" if kind == "article" else identity}
        version = capability.get("version") if kind == "image" else "article-text-v1"
        key = "comment_attachment:" + content_hash([kind, identity, version])
        cached = browsing.store.get(key)
        if isinstance(cached, dict) and cached.get("expires_at", 0) > time.time():
            observations.append(source | cached["observation"])
            continue
        try:
            async with asyncio.timeout(max(0, deadline - time.time())):
                if kind == "article" and hasattr(adapter, "read_comment_article"):
                    observation = await browsing._read(adapter.read_comment_article, identity)
                elif (kind == "image" and capability.get("images") and hasattr(adapter, "read_comment_image")):
                    data = await browsing._read(adapter.read_comment_image, identity)
                    downloaded_images[identity] = data
                    frames, coverage = await asyncio.to_thread(
                        sample_comment_image, data, max_frames=capability["max_frames"],
                        max_pixels=capability["max_pixels"], max_bytes=capability["max_bytes"],
                    )
                    captions = await provider.analyze_comment_images(task, frames, deadline=deadline)
                    if not isinstance(captions, list) or len(captions) != len(frames) or any(
                        not isinstance(text, str) or not text.strip() or len(text) > 1000 for text in captions
                    ):
                        raise CommentMaterialError("invalid", "视觉观察未覆盖实际输入的评论图像")
                    observation = {"coverage": coverage, "frames": [
                        {"text": text, **({"time_ms": frame["time_ms"]} if "time_ms" in frame else {})}
                        for frame, text in zip(frames, captions, strict=True)
                    ]}
                else:
                    raise CommentMaterialError("unavailable", "当前评论取材接口或视觉 Provider 不可用")
        except (ValueError, TimeoutError, httpx.HTTPError, OSError) as error:
            reason = getattr(error, "kind", "timeout" if isinstance(error, TimeoutError) else "unavailable")
            observations.append(source | {"status": "unavailable", "reason": reason})
            continue
        saved = observation | {"status": "read"}
        browsing.store.set(key, {"expires_at": time.time() + 6 * 3600, "observation": saved})
        observations.append(source | saved)
    if not (row or {}).get("preview") and (harvester := getattr(browsing.runtime, "meme_harvester", None)):
        try:
            harvester.enqueue_comment_attachments(
                [source_row],
                bvid=target.get("bvid"),
                dynamic_id=target.get("dynamic_id"),
                image_bytes_by_url=downloaded_images,
            )
        except Exception:
            pass
    return observations


def model_comment_materials(materials):
    """Expose only the bounded observation, never fetch URLs or internal errors."""
    return [{key: item[key] for key in ("kind", "status", "title", "text", "coverage", "frames") if key in item}
            for item in materials]


def validate_comment_material_sources(materials, items, comment_id):
    used = {item["source"] for item in materials if item.get("status") == "read"}
    if not used:
        return
    row = next((item for item in items if str(item.get("comment_id")) == str(comment_id)), {})
    available = set()
    for item in row.get("attachments", []):
        if item["kind"] == "article":
            available.add(f"https://www.bilibili.com/read/cv{item['article_id']}")
        elif item["kind"] == "image":
            available.add(item["url"])
    if not used <= available:
        raise ValueError("原始评论附件已变化，保留草稿")
