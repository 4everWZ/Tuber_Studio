"""Bounded lexical retrieval of local role artwork for an illustration decision."""

import re
import unicodedata
from collections.abc import Mapping
from itertools import islice


GALLERY_CHOICES = 12
EMOTIONS = {
    "idle": ("平常", "平静"),
    "cheerful": ("开心", "高兴", "快乐"),
    "angry": ("生气", "恼火"),
    "shy": ("害羞", "脸红"),
    "confused": ("困惑", "疑惑"),
    "excited": ("兴奋", "激动"),
}
WORDS = re.compile(r"[a-z0-9]{2,}|[\u3400-\u9fff]+")
OMIT = {"大肥", "肥鱼", "鱼鱼", "鲸鲸", "图片", "角色"}
MODEL_FIELDS = ("asset_id", "title", "emotions", "tags", "description")


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def _texts(value):
    if isinstance(value, str):
        yield value[:1500]
    elif isinstance(value, (Mapping, list, tuple)):
        for child in value.values() if isinstance(value, Mapping) else value:
            yield from _texts(child)


def _terms(text):
    terms = set()
    for token in WORDS.findall(text):
        if token[0].isascii():
            terms.add(token)
        else:
            terms.update(token[i:i + 2] for i in range(len(token) - 1))
    return terms - OMIT


def _score(asset, body, content):
    tags = {_normalize(tag) for tag in asset.get("tags", ())}
    title = _normalize(asset["title"])
    description = _normalize(asset.get("description", ""))
    title_terms, description_terms = _terms(title), _terms(description)
    body_text, body_terms = body
    context_text, context_terms = content
    # Draft wording is the strongest match, then original context. A tag hit
    # carries more weight than a shared pair of characters in a description.
    return sum(
        weight * (
            5 * sum(tag in text for tag in tags if tag not in OMIT and tag != "大肥鱼")
            + 4 * bool(title and title != "大肥鱼" and title in text)
            + 2 * min(5, len(title_terms & terms))
            + min(5, len(description_terms & terms))
            + sum(any(word in text for word in EMOTIONS.get(emotion, ())) for emotion in asset.get("emotions", ()))
        )
        for weight, text, terms in ((2, body_text, body_terms), (1, context_text, context_terms))
    )


def shortlist(items, draft, context):
    body = _normalize(draft[:6000])
    content = _normalize("\n".join(islice(_texts(context), 48))[:8000])
    body, content = (body, _terms(body)), (content, _terms(content))
    ranked = [(asset, _score(asset, body, content))
              for asset in items if asset.get("enabled", True) and not asset["missing"]]
    ranked.sort(key=lambda match: -match[1])
    matches = [asset for asset, score in ranked if score > 0]
    # Old entries can have only an English emotion label or a filename-like
    # title. Keep a small fallback pool and let the model also choose text.
    return matches[:GALLERY_CHOICES] or [asset for asset, _ in ranked[:4]]


def model_fields(asset):
    return {key: asset[key] for key in MODEL_FIELDS if key in asset and (key not in {"tags", "description"} or asset[key])}
