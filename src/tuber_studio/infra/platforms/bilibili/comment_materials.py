"""Bounded public evidence from attachments on a verified source comment."""

import json
import math
import re
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image, ImageOps

MAX_ATTACHMENTS = 3
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4 * 1024 * 1024
MAX_COMMENT_MATERIAL_SECONDS = 30
MAX_ARTICLE_CHARS = 6000
CDN_HOSTS = {"i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"}
ARTICLE_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
CV = re.compile(r"(?<![\w/])cv([1-9][0-9]{0,19})(?![0-9A-Za-z_])", re.I | re.ASCII)
URL = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9+.-]*://|//)[^\s<>\"'，。！？、；（）【】]+")
CDN_PATH = re.compile(r"/bfs/(?:[\w.-]+/)*[\w.-]+\.(?:jpe?g|png|gif|webp)", re.I | re.ASCII)


class CommentMaterialError(ValueError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


def _platform_url(value, hosts):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit("https:" + value if value.startswith("//") else value)
        if (
            parsed.scheme == "https" and parsed.hostname in hosts and parsed.port in (None, 443)
            and parsed.username is None and parsed.password is None
        ):
            return parsed
    except ValueError:
        pass
    return None


def comment_image_url(value):
    parsed = _platform_url(value, CDN_HOSTS)
    if parsed is None:
        return None
    # Presentation variants may be static thumbnails of an animated original.
    path = parsed.path.partition("@")[0]
    if CDN_PATH.fullmatch(path) and not {".", ".."}.intersection(path.split("/")):
        return f"https://{parsed.hostname}{path}"
    return None


def article_id(value):
    parsed = _platform_url(value, ARTICLE_HOSTS)
    if parsed is not None and (match := re.fullmatch(r"/read/cv([1-9][0-9]{0,19})/?", parsed.path)):
        return match[1]
    return None


def comment_attachments(content):
    """Normalize platform-declared pictures and standard Bilibili article references."""
    images, articles = [], []
    pictures = content.get("pictures")
    for item in pictures[:20] if isinstance(pictures, list) else []:
        if isinstance(item, dict) and (url := comment_image_url(item.get("img_src"))):
            images.append(url)
    text = content.get("message")
    if isinstance(text, str):
        text = text[:12000]
        for match in URL.finditer(text):
            if identity := article_id(match[0].rstrip(").,;!?]")):
                articles.append(identity)
        articles.extend(match[1] for match in CV.finditer(URL.sub(" ", text)))
    links = content.get("jump_url")
    for link in list(links.values())[:20] if isinstance(links, dict) else []:
        if isinstance(link, dict) and (identity := article_id(link.get("pc_url") or link.get("url"))):
            articles.append(identity)
    normalized = [
        *({"kind": "article", "article_id": value} for value in dict.fromkeys(articles)),
        *({"kind": "image", "url": value} for value in dict.fromkeys(images)),
    ]
    return normalized[:MAX_ATTACHMENTS]


def without_article_links(text, attachments):
    articles = {item.get("article_id") for item in attachments
                if isinstance(item, dict) and item.get("kind") == "article"}
    if not articles:
        return text
    return URL.sub(lambda match: " " if article_id(match[0].rstrip(").,;!?]")) in articles else match[0], text)


class _ArticleText(HTMLParser):
    _blocks = {"article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "pre", "tr"}
    _hidden = {"script", "style", "template", "noscript", "iframe"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in self._hidden:
            self.hidden.append(tag)
        if not self.hidden and tag in self._blocks:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        if not self.hidden and tag in self._blocks:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def article_excerpt(data, identity):
    if str(data.get("id")) != identity or data.get("private_pub", 0) not in (0, "0"):
        raise CommentMaterialError("invalid", "专栏对象不符或当前内容并非公开")
    source = data.get("content")
    if not isinstance(source, str) or not source:
        raise CommentMaterialError("empty", "专栏未返回可读正文")
    if data.get("type") == 3:
        try:
            ops = json.loads(source)["ops"]
            if not isinstance(ops, list):
                raise ValueError
            text = "".join(item["insert"] for item in ops if isinstance(item, dict)
                           and isinstance(item.get("insert"), str))
        except (ValueError, TypeError, KeyError, RecursionError):
            raise CommentMaterialError("invalid", "专栏正文结构无效") from None
    elif data.get("type") in (0, None):
        parser = _ArticleText()
        parser.feed(source)
        parser.close()
        text = "".join(parser.parts)
    else:
        raise CommentMaterialError("unsupported", "专栏正文格式暂不支持")
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        raise CommentMaterialError("empty", "专栏未返回可读文字")
    title = data.get("title")
    return {"title": title[:200] if isinstance(title, str) else "", "text": text[:MAX_ARTICLE_CHARS],
            "coverage": "excerpt" if len(text) > MAX_ARTICLE_CHARS else "text"}


def _frame_bytes(image, max_pixels, max_bytes):
    edge = min(1024, math.isqrt(max_pixels))
    image = image.convert("RGB")
    image.thumbnail((edge, edge))
    for quality in (83, 65, 45):
        output = BytesIO()
        image.save(output, "JPEG", quality=quality)
        if output.tell() <= max_bytes:
            return {"image": output.getvalue(), "width": image.width, "height": image.height,
                    "mime_type": "image/jpeg"}
        image.thumbnail((max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)))
    raise CommentMaterialError("too_large", "评论图片超出视觉字节上限")


def sample_comment_image(data, *, max_frames, max_pixels, max_bytes):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise CommentMaterialError("too_large", "评论图片超出字节上限")
    try:
        with Image.open(BytesIO(data), formats=("JPEG", "PNG", "GIF", "WEBP")) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise CommentMaterialError("too_large", "评论图片像素超出上限")
            if image.format != "GIF":
                return [_frame_bytes(ImageOps.exif_transpose(image), max_pixels, max_bytes)], "still_image"
            sample_count = min(3, max_frames)
            # Walk a bounded prefix without loading n_frames, which scans the whole GIF.
            frames, timestamp = [], 0
            for number in range(12):
                try:
                    image.seek(number)
                except EOFError:
                    break
                if timestamp >= 6000:
                    break
                frames.append((number, timestamp))
                duration = image.info.get("duration", 100)
                timestamp += max(10, duration if type(duration) is int else 100)
            count = min(sample_count, len(frames))
            indices = [0] if count == 1 else [i * (len(frames) - 1) // (count - 1) for i in range(count)]
            result = []
            for index in indices:
                number, timestamp = frames[index]
                image.seek(number)
                result.append(_frame_bytes(image, max_pixels, max_bytes // count) | {"time_ms": timestamp})
            return result, "sampled_gif"
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise CommentMaterialError("invalid", "评论图片无法安全解码") from None

