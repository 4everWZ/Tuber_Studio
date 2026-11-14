"""Build a shareable card from the selected dynamic's public comment sources."""

import json
import re
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from .operations.browsing.comment_timeline import comment_thread
from .operations.browsing.thread_context import COMMENT_ID, public_discussion_url

VIDEO = re.compile(r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})/?")
MAX_QUOTES = 6
MAX_CARD_PAGES = 9


def _references(source):
    if source.get("type") == "public_incident":
        return source.get("public_sources") or []
    if source.get("type") == "published_reply" and (video := VIDEO.fullmatch(source.get("source_url", ""))):
        original = source.get("original_comment") or {}
        return [{"bvid": video[1], "comment_id": original.get("id"), "quote": original.get("text")}]
    return []


def _legacy_card(sources):
    for source in sources:
        comments, links = [], {}
        for ref in _references(source):
            bvid, comment_id, quote = (ref.get(key) for key in ("bvid", "comment_id", "quote"))
            url = public_discussion_url(bvid, comment_id) if COMMENT_ID.fullmatch(str(comment_id)) else None
            if url and isinstance(quote, str) and quote.strip():
                comments.append({"quote": quote.strip(), "url": url, "author": "作者资料未保存"})
                links.setdefault(url, {"bvid": bvid, "url": url})
        if comments:
            return {"comments": comments, "links": list(links.values()), "omitted": 0, "legacy": True}
    return None


def comment_card(sources, *, store=None, actor=None, legacy=False, timezone="Asia/Taipei"):
    sources = sorted(sources, key=lambda item: item.get("type") != "public_incident")
    if legacy:
        return _legacy_card(sources)
    entries, links, missing = {}, {}, set()
    actor = actor or {}

    def add(bvid, identifier, quote, *, author=None, uid=None, created_at=None, parent=None):
        if not COMMENT_ID.fullmatch(str(identifier)) or not isinstance(quote, str) or not quote.strip():
            return
        url = public_discussion_url(bvid, identifier)
        if not url:
            return
        entries.setdefault(url, {
            "quote": quote, "url": url, "author": author or "作者资料未保存",
            "uid": str(uid) if uid else None, "created_at": _time(created_at),
            "comment_id": str(identifier), "parent_comment_id": str(parent) if parent else None,
        })
        return url

    for source in sources:
        published = _published(store, source) if source.get("type") == "published_reply" else {}
        for ref in _references(source):
            bvid, comment_id, quote = (ref.get(key) for key in ("bvid", "comment_id", "quote"))
            original = _original(store, bvid, comment_id, quote, ref.get("thread_id"))
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
            root = ref.get("thread_id") or published.get("root") or original.get("thread_id")
            root = root or original.get("parent_comment_id") or (str(comment_id) if original else None)
            parent = captured.get("parent") or {}
            if anchor and str(parent.get("comment_id")) == str(anchor.get("parent_comment_id")):
                add(bvid, parent.get("comment_id"), parent.get("text"), author=parent.get("author_name"),
                    uid=parent.get("author_uid"), created_at=parent.get("created_at"), parent=parent.get("parent_comment_id"))
            if store is not None and root:
                thread = comment_thread(
                    store, bvid, root, account_id=actor.get("account_uid") or published.get("account_id"),
                    character_id=actor.get("character_id") or published.get("character_id"),
                )
                known = {row["comment_id"]: row for row in thread["comments"]}
                known_anchor = known.get(str(comment_id))
                if known_anchor and known_anchor["text"] == quote:
                    item = entries[url]
                    item.update(author=known_anchor["author_name"] or item["author"],
                                uid=known_anchor["author_uid"] or item["uid"],
                                created_at=known_anchor["created_at"] or item["created_at"],
                                parent_comment_id=known_anchor["parent_comment_id"] or item["parent_comment_id"])
                selected = {str(comment_id)}

                def add_ancestor(identifier):
                    if identifier in selected:
                        return None
                    selected.add(identifier)
                    item = known.get(identifier)
                    if not item:
                        legacy = _original(store, bvid, identifier, None, None)
                        if legacy and str(legacy.get("thread_id") or identifier) in {str(root), str(identifier)}:
                            item = {"author_name": legacy["display_name"], "author_uid": legacy["platform_user_id"],
                                    "text": legacy["content"], "created_at": legacy["created_at"],
                                    "parent_comment_id": legacy["parent_comment_id"]}
                    if not item:
                        if public_discussion_url(bvid, identifier) not in entries:
                            missing.add(public_discussion_url(bvid, identifier))
                        return None
                    add(bvid, identifier, item["text"], author=item["author_name"], uid=item["author_uid"],
                        created_at=item["created_at"], parent=item["parent_comment_id"])
                    return item["parent_comment_id"]

                ancestor = entries[url]["parent_comment_id"]
                while ancestor:
                    ancestor = add_ancestor(ancestor)
                if str(root) not in selected:
                    add_ancestor(str(root))
                descendants = {str(comment_id)}
                remaining = [item for item in thread["comments"] if item["text"]]
                while remaining:
                    matches = [item for item in remaining if item["parent_comment_id"] in descendants
                               and item["comment_id"] not in descendants]
                    if not matches:
                        break
                    descendants.update(item["comment_id"] for item in matches)
                    remaining = [item for item in remaining if item["comment_id"] not in descendants]
                # Keep confirmed outcomes and the comments needed to understand them.
                outcomes = [item for item in thread["comments"] if item["publication"] == "confirmed"
                            and item["comment_id"] in descendants]
                for outcome in outcomes[-2:]:
                    identifier = outcome["comment_id"]
                    while identifier:
                        identifier = add_ancestor(identifier)
                # Keep a short continuation when no confirmed reply followed the anchor.
                if not outcomes:
                    identifier = str(comment_id)
                    for _ in range(3):
                        child = next((item for item in thread["comments"] if item["parent_comment_id"] == identifier
                                      and item["comment_id"] not in selected and item["text"]), None)
                        if not child:
                            break
                        identifier = child["comment_id"]
                        add_ancestor(identifier)
            if source.get("type") == "published_reply":
                identity = published.get("generation_identity") or {}
                author = identity.get("account_display_name")
                uid = identity.get("account_uid") or published.get("account_id")
                if not author and uid and str(uid) == str(actor.get("account_uid")):
                    author = actor.get("account_display_name")
                add(bvid, source.get("receipt_id"), source.get("text"), author=author, uid=uid,
                    created_at=published.get("published_at"), parent=comment_id)
    if not entries:
        return None
    all_times = all(item["created_at"] for item in entries.values())
    selected = sorted(entries.values(), key=lambda item: (
        item["created_at"] or 0, int(item["comment_id"]),
    ) if all_times else (int(item["comment_id"]),))[:MAX_QUOTES * MAX_CARD_PAGES]
    zone = ZoneInfo(timezone)
    for item in selected:
        parent_id = item["parent_comment_id"]
        parent = entries.get(item["url"].split("#", 1)[0] + f"#reply{parent_id}") if parent_id else None
        item["reply_to"] = parent["author"] if parent else None
        item["time_label"] = (datetime.fromtimestamp(item["created_at"], zone).strftime("%Y-%m-%d %H:%M")
                              if item["created_at"] else None)
    return {"version": 3, "comments": selected, "links": list(links.values()),
            "omitted": len(entries) - len(selected), "missing": len(missing - entries.keys()),
            "page_count": (len(selected) + MAX_QUOTES - 1) // MAX_QUOTES}


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
            "SELECT account_id,character_id,target,updated_at AS published_at FROM browsing_intents WHERE id=? AND status='published'",
            (identity.removeprefix("reply:browsing:"),),
        )
        return row | json.loads(row["target"]) if row else {}
    row = store.one(
        "SELECT character_id,answer_check,updated_at AS published_at FROM drafts WHERE id=? AND status='published'",
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


def render_comment_card(card, page=0):
    if not 0 <= page < card.get("page_count", 1):
        raise ValueError("原评论图页码无效")
    if card.get("legacy"):
        quotes = [" ".join(comment["quote"].split()) for comment in card["comments"][:4]]
        return _render(tuple(quote[:177] + "…" if len(quote) > 178 else quote for quote in quotes))
    comments = card["comments"]
    if card.get("version") == 3:
        entries = tuple((item["author"], item["uid"], item["quote"], item["parent_comment_id"],
                         item["reply_to"], item["time_label"])
                        for item in comments[page * MAX_QUOTES:(page + 1) * MAX_QUOTES])
        return _render_thread(entries, page + 1, card["page_count"],
                              card["omitted"] if page + 1 == card["page_count"] else 0,
                              card["missing"] if page == 0 else 0)
    if card.get("version") == 2:
        entries = tuple((item["author"], item["uid"], item["quote"], item["parent_comment_id"] is not None)
                        for item in comments[page * MAX_QUOTES:(page + 1) * MAX_QUOTES])
        return _render_discussion(entries, card["omitted"] if page + 1 == card["page_count"] else 0,
                                  page + 1, card["page_count"], card["missing"] if page == 0 else 0)
    entries = tuple((item["author"], item["uid"], item["quote"], item["parent_comment_id"] is not None)
                    for item in comments)
    return _render_discussion(entries, card["omitted"])


@lru_cache(maxsize=16)
def _render_thread(entries, page, pages, omitted, missing):
    width = 960
    body, meta = _font(28), _font(21)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    rows = []
    for author, uid, quote, parent, recipient, posted_at in entries:
        left = 76 if parent else 34
        heading = author[:40] + (f"   UID {uid}" if uid else "")
        recipient = (f"回复 @{recipient[:40]}" if recipient else f"回复评论 {parent}") if parent else ""
        text = quote.strip()
        if len(text) > 800:
            text = text[:799] + "…"
        lines = [line for paragraph in text.splitlines()
                 for line in _wrap(measure, paragraph, body, width - left - 34)]
        if len(lines) > 30:
            lines = [*lines[:29], "…（全文见原评论）"]
        rows.append((left, heading, recipient, lines, posted_at))
    note = []
    if omitted:
        note.append(f"另有 {omitted} 条，见原评论链接")
    if missing:
        note.append(f"有 {missing} 条关键原评未取得")
    height = 90 + sum(59 + 34 * bool(to) + 38 * len(lines) + 34 * bool(at)
                      for _, _, to, lines, at in rows) + len(note) * 33
    canvas = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(canvas)
    draw.text((34, 20), "评论摘录", fill="#20242b", font=body)
    if pages > 1:
        draw.text((width - 34, 28), f"{page} / {pages}", anchor="ra", fill="#646b76", font=meta)
    y = 69
    for left, heading, recipient, lines, posted_at in rows:
        draw.line((left, y, width - 34, y), fill="#e5e8ed")
        draw.text((left, y + 14), heading, fill="#505862", font=meta)
        y += 45
        if recipient:
            draw.text((left, y), recipient, fill="#096fa2", font=meta)
            y += 34
        for line in lines:
            draw.text((left, y), line, fill="#20242b", font=body)
            y += 38
        if posted_at:
            draw.text((left, y + 2), posted_at, fill="#646b76", font=meta)
            y += 34
        y += 14
    for line in note:
        draw.text((34, y), line, fill="#646b76", font=meta)
        y += 33
    image = BytesIO()
    canvas.save(image, format="PNG")
    return image.getvalue()


@lru_cache(maxsize=16)
def _render_discussion(entries, omitted, page=1, pages=1, missing=0):
    canvas = Image.new("RGB", (960, 3600), "#f1f4f8")
    draw = ImageDraw.Draw(canvas)
    draw.text((46, 32), "公开评论", fill="#1d324d", font=_font(36))
    if pages > 1:
        draw.text((760, 40), f"{page} / {pages}", fill="#526476", font=_font(28))
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
    if missing:
        draw.text((48, y + 10), f"有 {missing} 条关键原评未取得", fill="#526476", font=_font(28))
        y += 52
    image = BytesIO()
    canvas.crop((0, 0, 960, y + 20)).save(image, format="PNG")
    return image.getvalue()
