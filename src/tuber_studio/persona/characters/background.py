"""Select verbatim passages from the current character card for private conversation."""

import json
import re

from ..memory.daily import front_topic_keywords
from .front_persona import without_address

_PAST = re.compile(
    r"(?:(?:你的|角色的)(?:过去|过往|经历|故事|背景|身世|来历)|"
    r"你(?:以前|曾经|小时候|过去)?(?:都)?(?:经历过|经历了)(?:哪些|什么)|"
    r"你(?:以前|曾经|小时候|过去)?有(?:过)?(?:哪些|什么)(?:难忘的)?(?:经历|故事|趣事)|"
    r"(?:以前|小时候|过去|曾经)(?:发生过什么|的事|的经历|的故事|是什么样)|"
    r"(?:讲讲|聊聊|说说)(?:你)?(?:小时候|以前|过去)|(?:以前|小时候|过去)的你.{0,3}什么样)"
    r"(?:是什么样的?|的事)?(?:吗|呢|吧|呀)?(?=[，,。！？!?\s]|$)"
)
_HOME = re.compile(r"你(?:平时|以前)?住(?:在)?哪|你的家(?:里)?(?:什么样|是什么样)")
_HOME_TEXT = re.compile(r"住在|居住|家在|家住|生活在")
_APPEARANCE = re.compile(r"长什么样|什么模样|外貌|外观|长相|外表|穿着打扮")
_HABIT = re.compile(r"口头禅|口头习惯|常挂在嘴边|怎么自称")
_HABIT_TEXT = re.compile(r"口头禅|挂在嘴边|自称|常说|常喊")
_LOOK_TEXT = re.compile(r"头发|长发|短发|发色|眼睛|长着|身穿|穿着|外形|外貌|打扮")
_HISTORICAL = re.compile(r"曾经|曾在|以前|当年|那年|小时候|后来|第一次|起初")
_BACKGROUND_FRAMING = re.compile(r"的时候|时候|过去|经历|过往|朋友|风格|身份|关系|你好|主人|打招呼|聊天")
_FOLLOWUP = re.compile(
    r"(?:那|它|你)?(?:后来|然后|接着|之后|当时|那次|那时候|那件事|刚才那件事)"
    r"(?:后来|之后)?(?:怎样了?|怎么样了?|发生了什么|为什么(?:会)?这样)?(?:呢|吗)?|"
    r"(?:再|继续)(?:讲讲|说说|讲|说)"
)
_PERSONA_QUESTION = re.compile(
    r"你(?:到底)?是谁|你(?:的)?(?:身份|名字|说话风格|语气)是什么|"
    r"(?:我们|咱们|我和你|你和我)[^，,。？！?!]{0,6}(?:什么关系|是朋友吗)|"
    r"你(?:平时|一般)?(?:怎么说话|说话[^，,。？！?!]{0,4}风格)"
)
_TEXT_FIELDS = ("text", "description", "content", "story", "summary", "正文")
_EXPERIENCE_FIELDS = ("experiences", "experience", "history", "经历")
_PASSAGE = re.compile(r"[^。！？!?\r\n]{1,160}[。！？!?]?|[。！？!?]")


def _entries(value, labels=()):
    if isinstance(value, str):
        yield value, labels
    elif isinstance(value, list):
        for entry in value:
            yield from _entries(entry, labels)
    elif isinstance(value, dict):
        anchors = list(labels)
        for key in ("title", "name", "keywords", "tags"):
            raw = value.get(key, [])
            anchors.extend(word for word in (raw if isinstance(raw, list) else [raw]) if isinstance(word, str) and word)
        for field in (*_TEXT_FIELDS, "background", *_EXPERIENCE_FIELDS):
            if field in value:
                yield from _entries(value[field], anchors)


def _matches(term, text):
    if term.isascii() and term.isalnum():
        return re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text, re.I) is not None
    return term.casefold() in text.casefold()


def _card_sources(character):
    core = character.get("core") or {}
    for field in _EXPERIENCE_FIELDS:
        yield core.get(field), "experience"
    yield core.get("background"), "background"
    yield character.get("lore"), "lore"
    yield core.get("identity"), "identity"
    appearance = core.get("appearance")
    if isinstance(appearance, dict):
        for key, value in appearance.items():
            if key not in {"source", "sources", "url", "image", "id"}:
                yield value, "appearance"
    else:
        yield appearance, "appearance"
    yield core.get("traits"), "traits"
    yield (character.get("style") or {}).get("catchphrases"), "habit"
    yield (character.get("style") or {}).get("instructions"), "style"


def _last_topic(history):
    parts = []
    for turn in reversed(history):
        role, content = turn.get("role"), turn.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            break
        if turn.get("kind") not in {None, "viewer", "reply"}:
            break
        if turn.get("kind") == "viewer" and content.startswith("当前号主消息："):
            try:
                content = json.loads(content.removeprefix("当前号主消息："))["text"]
            except (ValueError, KeyError, TypeError):
                break
        parts.append(content)
        if role == "user" or len(parts) == 2:
            break
    return " ".join(reversed(parts))


def front_background(character, query: str, *, history=()) -> list[str]:
    """History may supply the referent of a follow-up, never the returned text."""
    query = _PERSONA_QUESTION.sub(" ", without_address(query, character)).strip(" ，,。！？!?～~\n")
    if _FOLLOWUP.fullmatch(query):
        # Take the last exchange only. An earlier topic cannot cross a change
        # of subject or replace an absent/disabled conversation window.
        query = _PERSONA_QUESTION.sub(" ", without_address(_last_topic(history), character))
    if not query:
        return []
    topics = front_topic_keywords(_BACKGROUND_FRAMING.sub(" ", query))
    general = bool(_PAST.search(query))
    home = bool(_HOME.search(query))
    appearance = bool(_APPEARANCE.search(query))
    habit = bool(_HABIT.search(query))
    ranked = {}
    for value, kind in _card_sources(character):
        if kind == "habit" and not habit:
            continue
        for raw, labels in _entries(value):
            label = max((len(key) for key in labels if _matches(key, query)
                         and (len(key) == 1 or front_topic_keywords(key))), default=0)
            for index, match in enumerate(_PASSAGE.finditer(raw)):
                passage = match[0].strip()
                if not passage or not any(char.isalnum() for char in passage):
                    continue
                if kind == "style" and not (habit and _HABIT_TEXT.search(passage)):
                    continue
                overlap = max((len(term) for term in topics if _matches(term, passage)), default=0)
                fallback = (general and (kind == "experience" or _HISTORICAL.search(passage))
                            or kind == "background" and (general or home)
                            or home and kind == "identity" and _HOME_TEXT.search(passage)
                            or appearance and (kind == "appearance" or kind == "identity" and _LOOK_TEXT.search(passage))
                            or habit and (kind == "habit" or _HABIT_TEXT.search(passage)))
                if label and index == 0 or overlap or fallback:
                    score = (max(label, overlap), kind in {"background", "experience", "lore"}, overlap,
                             bool(label), general and kind == "experience",
                             home and kind == "background" or habit and kind == "habit",
                             kind == "background")
                    if passage not in ranked or score > ranked[passage]:
                        ranked[passage] = score
    return sorted(ranked, key=ranked.get, reverse=True)[:2]
