"""Build a shareable card from the selected dynamic's public comment sources."""

import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VIDEO = re.compile(r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})/?")


def comment_card(sources):
    for source in sorted(sources, key=lambda item: item.get("type") != "public_incident"):
        if source.get("type") == "public_incident":
            refs = source.get("public_sources") or []
        elif source.get("type") == "published_reply" and (video := VIDEO.fullmatch(source.get("source_url", ""))):
            original = source.get("original_comment") or {}
            refs = [{"bvid": video[1], "comment_id": original.get("id"), "quote": original.get("text")}]
        else:
            continue
        comments = []
        for ref in refs:
            bvid, comment_id, quote = (ref.get(key) for key in ("bvid", "comment_id", "quote"))
            if (
                re.fullmatch(r"BV[0-9A-Za-z]{10}", str(bvid))
                and re.fullmatch(r"[1-9][0-9]{0,19}", str(comment_id))
                and isinstance(quote, str)
                and quote.strip()
            ):
                comments.append({
                    "quote": quote.strip(),
                    "url": f"https://www.bilibili.com/video/{bvid}#reply{comment_id}",
                })
        if comments:
            return {"comments": comments}
    return None


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
    quotes = [" ".join(comment["quote"].split()) for comment in card["comments"][:4]]
    return _render(tuple(quote[:177] + "…" if len(quote) > 178 else quote for quote in quotes))
