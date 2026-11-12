"""Build a shareable card from the selected dynamic's public comment sources."""

import json
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .operations.browsing.thread_context import COMMENT_ID, public_discussion_url

VIDEO = re.compile(r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})/?")
MAX_QUOTES = 6


def comment_card(sources, *, store=None, actor=None, legacy=False):
    entries, links = {}, {}
    actor = actor or {}

    def add(bvid, identifier, quote, *, author=None, uid=None, created_at=None, parent=None):
        if not COMMENT_ID.fullmatch(str(identifier)) or not isinstance(quote, str) or not quote.strip():
            return
        url = public_discussion_url(bvid, identifier)
        if not url:
            return
        entries.setdefault(url, {
            "quote": quote.strip(), "url": url, "author": author or "作者资料未保存",
            "uid": str(uid) if uid else None, "created_at": _time(created_at),
            "comment_id": str(identifier), "parent_comment_id": str(parent) if parent else None,
        })
        return url

    for source in sorted(sources, key=lambda item: item.get("type") != "public_incident"):
        if source.get("type") == "public_incident":
            refs = source.get("public_sources") or []
        elif source.get("type") == "published_reply" and (video := VIDEO.fullmatch(source.get("source_url", ""))):
            original = source.get("original_comment") or {}
            refs = [{"bvid": video[1], "comment_id": original.get("id"), "quote": original.get("text")}]
        else:
            continue
        for ref in refs:
            bvid, comment_id, quote = (ref.get(key) for key in ("bvid", "comment_id", "quote"))
            original = _original(store, bvid, comment_id, quote, ref.get("thread_id"))
            published = _published(store, source) if source.get("type") == "published_reply" and not legacy else {}
            captured = published.get("source_discussion") or {}
            anchor = captured.get("source") or {}
            if str(anchor.get("comment_id")) != str(comment_id) or anchor.get("text") != quote:
                anchor = {}
            url = add(
                bvid, comment_id, quote,
                author=original.get("display_name") or anchor.get("author_name") or published.get("recipient_name"),
                uid=original.get("platform_user_id") or anchor.get("author_uid") or published.get("recipient"),
                created_at=original.get("created_at") or anchor.get("created_at"),
                parent=original.get("parent_comment_id") or anchor.get("parent_comment_id"),
            )
            if not url:
                continue
            links.setdefault(bvid, {"bvid": bvid, "url": url})
            if original.get("parent_comment_id"):
                parent = _original(store, bvid, original["parent_comment_id"], None, original.get("thread_id"))
                add(bvid, parent.get("comment_id"), parent.get("content"), author=parent.get("display_name"),
                    uid=parent.get("platform_user_id"), created_at=parent.get("created_at"),
                    parent=parent.get("parent_comment_id"))
            parent = captured.get("parent") or {}
            if anchor and str(parent.get("comment_id")) == str(anchor.get("parent_comment_id")):
                add(bvid, parent.get("comment_id"), parent.get("text"), author=parent.get("author_name"),
                    uid=parent.get("author_uid"), created_at=parent.get("created_at"), parent=parent.get("parent_comment_id"))
            if source.get("type") == "published_reply" and not legacy:
                identity = published.get("generation_identity") or {}
                author = identity.get("account_display_name")
                uid = identity.get("account_uid") or published.get("account_id")
                if not author and uid and str(uid) == str(actor.get("account_uid")):
                    author = actor.get("account_display_name")
                add(bvid, source.get("receipt_id"), source.get("text"), author=author, uid=uid,
                    created_at=published.get("published_at"), parent=comment_id)
        if legacy and entries:
            return {"comments": list(entries.values()), "links": list(links.values()), "omitted": 0, "legacy": True}
    if not entries:
        return None
    # Numeric platform IDs preserve ordering when historical sources lack timestamps.
    selected = list(entries.values())[:MAX_QUOTES]
    by_video = {video: index for index, video in enumerate(links)}
    use_time = all(item["created_at"] for item in selected)
    selected.sort(key=lambda item: (
        by_video[item["url"].split("/")[-1].split("#")[0]],
        item["created_at"] if use_time else 0, int(item["comment_id"]),
    ))
    return {"comments": selected, "links": list(links.values()), "omitted": len(entries) - len(selected)}


def _time(value):
    try:
        timestamp = float(value)
        return timestamp if 0 < timestamp < 253402300800 else None
    except (TypeError, ValueError):
        return None


def _original(store, bvid, identifier, quote, thread):
    if store is None:
        return {}
    row = store.one(
        "SELECT comment_id,thread_id,parent_comment_id,platform_user_id,display_name,content,created_at "
        "FROM comments WHERE platform='bilibili' AND video_id=? AND comment_id=?", (bvid, identifier),
    )
    if not row or (thread and str(row["thread_id"]) != str(thread)) or (quote is not None and row["content"] != quote):
        return {}
    return row


def _published(store, source):
    if store is None:
        return {}
    identity = source["id"]
    if identity.startswith("reply:browsing:"):
        row = store.one(
            "SELECT account_id,target,updated_at AS published_at FROM browsing_intents WHERE id=? AND status='published'",
            (identity.removeprefix("reply:browsing:"),),
        )
        return row | json.loads(row["target"]) if row else {}
    row = store.one(
        "SELECT answer_check,updated_at AS published_at FROM drafts WHERE id=? AND status='published'",
        (identity.removeprefix("reply:"),),
    )
    if not row:
        return {}
    row["generation_identity"] = json.loads(row["answer_check"] or "{}").get("generation_identity")
    return row


@lru_cache(maxsize=4)
def _font(size):
    for path in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ):
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise ValueError("生成原评论图需要安装系统中文字体，如 Noto Sans CJK")


def _wrap(draw, text, font, width):
    lines, current = [], ""
    for char in text:
        if current and draw.textlength(current + char, font=font) > width:
            lines.append(current)
            current = ""
        current += char
    return [*lines, current]


@lru_cache(maxsize=16)
def _render(quotes):
    canvas = Image.new("RGB", (960, 1850), "#eaf1fb")
    draw = ImageDraw.Draw(canvas)
    draw.text((48, 36), "评论区原话", fill="#1d324d", font=_font(36))
    y = 106
    for quote in quotes:
        lines = _wrap(draw, quote, _font(28), 780)
        height = 48 + 43 * len(lines)
        draw.rounded_rectangle((48, y, 912, y + height), radius=20, fill="#ffffff")
        for number, line in enumerate(lines):
            draw.text((76, y + 21 + number * 43), line, fill="#23364a", font=_font(28))
        y += height + 16
    image = BytesIO()
    canvas.crop((0, 0, 960, y + 24)).save(image, format="PNG")
    return image.getvalue()


def render_comment_card(card):
    if card.get("legacy"):
        quotes = [" ".join(comment["quote"].split()) for comment in card["comments"][:4]]
        return _render(tuple(quote[:177] + "…" if len(quote) > 178 else quote for quote in quotes))
    entries = tuple((item["author"], item["uid"], item["quote"], item["parent_comment_id"] is not None)
                    for item in card["comments"])
    return _render_discussion(entries, card["omitted"])


@lru_cache(maxsize=16)
def _render_discussion(entries, omitted):
    canvas = Image.new("RGB", (960, 3600), "#f1f4f8")
    draw = ImageDraw.Draw(canvas)
    draw.text((46, 32), "公开评论", fill="#1d324d", font=_font(36))
    y = 96
    for author, uid, quote, reply in entries:
        left = 78 if reply else 46
        quote = " ".join(quote.split())
        quote = quote[:219] + "…" if len(quote) > 220 else quote
        heading = author[:24] + (f"   UID {uid}" if uid else "")
        lines = _wrap(draw, quote, _font(28), 840 - left)
        height = 80 + 42 * len(lines)
        draw.rounded_rectangle((left, y, 914, y + height), radius=18, fill="#ffffff")
        draw.text((left + 24, y + 17), heading, fill="#1d4a71", font=_font(28))
        for number, line in enumerate(lines):
            draw.text((left + 24, y + 65 + number * 42), line, fill="#23364a", font=_font(28))
        y += height + 13
    if omitted:
        draw.text((48, y + 10), f"另有 {omitted} 条，见原楼链接", fill="#526476", font=_font(28))
        y += 52
    image = BytesIO()
    canvas.crop((0, 0, 960, y + 20)).save(image, format="PNG")
    return image.getvalue()
