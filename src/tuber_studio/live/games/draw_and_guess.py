"""Built-in Draw & Guess 2.0 mini-game for Tuber Studio with safe SVG sanitization and LLM support."""

from __future__ import annotations

import json
import math
import random
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any, Literal

from .draw_word_bank import WORD_BANK

CANVAS_WIDTH = 400
CANVAS_HEIGHT = 300
MAX_STROKES = 32

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_UNSAFE_VALUE_RE = re.compile(
    r"(?:<\s*/?\s*script|javascript:|vbscript:|data:|url\s*\(|https?://|expression\s*\(|\bon[a-z]+\s*=)",
    re.IGNORECASE,
)
_PATH_ALLOWED_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9.,+\-\s]+$")
_PATH_TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")
_NUMBER_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _is_unsafe_str(value: Any) -> bool:
    return isinstance(value, str) and bool(_UNSAFE_VALUE_RE.search(value))


def _clamp_number(value: Any, low: float, high: float, *, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            if isinstance(value, str) and not _is_unsafe_str(value):
                value = float(value.strip())
            else:
                return default
        except (TypeError, ValueError):
            return default
    num = float(value)
    if not math.isfinite(num):
        return default
    clamped = max(low, min(high, num))
    return int(clamped) if clamped.is_integer() else round(clamped, 2)


def _sanitize_color(value: Any, *, default: str) -> str:
    if not isinstance(value, str):
        return default
    cleaned = value.strip()
    if not cleaned or _is_unsafe_str(cleaned):
        return default
    if cleaned.lower() == "none":
        return "none"
    if _HEX_COLOR_RE.fullmatch(cleaned):
        return cleaned.lower()
    return default


def _format_num(num: float) -> str:
    if isinstance(num, int) or float(num).is_integer():
        return str(int(num))
    return f"{num:.2f}".rstrip("0").rstrip(".")


def _sanitize_path_d(raw_d: Any) -> str | None:
    if not isinstance(raw_d, str):
        return None
    cleaned = raw_d.strip()
    if not cleaned or _is_unsafe_str(cleaned) or not _PATH_ALLOWED_RE.fullmatch(cleaned):
        return None
    tokens = _PATH_TOKEN_RE.findall(cleaned)
    if not tokens or not any(tok.isalpha() for tok in tokens):
        return None

    out: list[str] = []
    cmd = ""
    arg_idx = 0
    for token in tokens:
        if token.isalpha():
            cmd = token
            arg_idx = 0
            out.append(cmd)
            continue
        try:
            val = float(token)
        except ValueError:
            return None
        if not math.isfinite(val):
            return None

        if cmd == "H":
            val = max(0.0, min(float(CANVAS_WIDTH), val))
        elif cmd == "V":
            val = max(0.0, min(float(CANVAS_HEIGHT), val))
        elif cmd in {"M", "L", "T"}:
            limit = CANVAS_WIDTH if arg_idx % 2 == 0 else CANVAS_HEIGHT
            val = max(0.0, min(float(limit), val))
        elif cmd in {"C", "S", "Q"}:
            limit = CANVAS_WIDTH if arg_idx % 2 == 0 else CANVAS_HEIGHT
            val = max(0.0, min(float(limit), val))
        elif cmd == "A":
            slot = arg_idx % 7
            if slot in {0, 5}:
                val = max(0.0, min(float(CANVAS_WIDTH), val))
            elif slot in {1, 6}:
                val = max(0.0, min(float(CANVAS_HEIGHT), val))
            elif slot in {3, 4}:
                val = 1.0 if val >= 0.5 else 0.0
            else:
                val = max(-360.0, min(360.0, val))
        elif cmd in {"h"}:
            val = max(-float(CANVAS_WIDTH), min(float(CANVAS_WIDTH), val))
        elif cmd in {"v"}:
            val = max(-float(CANVAS_HEIGHT), min(float(CANVAS_HEIGHT), val))
        elif cmd in {"m", "l", "t", "c", "s", "q"}:
            limit = CANVAS_WIDTH if arg_idx % 2 == 0 else CANVAS_HEIGHT
            val = max(-float(limit), min(float(limit), val))
        elif cmd == "a":
            slot = arg_idx % 7
            if slot in {0, 5}:
                val = max(-float(CANVAS_WIDTH), min(float(CANVAS_WIDTH), val))
            elif slot in {1, 6}:
                val = max(-float(CANVAS_HEIGHT), min(float(CANVAS_HEIGHT), val))
            elif slot in {3, 4}:
                val = 1.0 if val >= 0.5 else 0.0
            else:
                val = max(-360.0, min(360.0, val))
        arg_idx += 1
        out.append(_format_num(val))
    return " ".join(out) if out else None


def _sanitize_polygon_points(raw_points: Any) -> str | None:
    nums: list[float] = []
    if isinstance(raw_points, str):
        if _is_unsafe_str(raw_points):
            return None
        if not re.fullmatch(r"[0-9.,+\-\s]+", raw_points.strip()):
            return None
        for match in _NUMBER_RE.findall(raw_points):
            val = float(match)
            if not math.isfinite(val):
                return None
            nums.append(val)
    elif isinstance(raw_points, list):
        for pair in raw_points:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                x = _clamp_number(pair[0], 0, CANVAS_WIDTH)
                y = _clamp_number(pair[1], 0, CANVAS_HEIGHT)
                if x is None or y is None:
                    return None
                nums.extend([float(x), float(y)])
            elif isinstance(pair, (int, float)) and not isinstance(pair, bool):
                nums.append(float(pair))
            else:
                return None
    else:
        return None

    if len(nums) < 6 or len(nums) % 2 != 0:
        return None

    pairs: list[str] = []
    for idx in range(0, len(nums), 2):
        x = max(0.0, min(float(CANVAS_WIDTH), nums[idx]))
        y = max(0.0, min(float(CANVAS_HEIGHT), nums[idx + 1]))
        pairs.append(f"{_format_num(x)},{_format_num(y)}")
    return " ".join(pairs)


def sanitize_strokes(raw_strokes: Any) -> list[dict[str, Any]]:
    """Validate and sanitize SVG strokes to 6 safe primitives inside a 400x300 viewBox."""
    if not isinstance(raw_strokes, list):
        return []

    sanitized: list[dict[str, Any]] = []
    for item in raw_strokes:
        if len(sanitized) >= MAX_STROKES:
            break
        if not isinstance(item, dict):
            continue
        if any(_is_unsafe_str(k) for k in item):
            continue

        stroke_type = str(item.get("type", "")).strip().lower()
        if stroke_type == "path":
            d = _sanitize_path_d(item.get("d"))
            if not d:
                continue
            width = _clamp_number(item.get("width", 4), 0.5, 24, default=4)
            sanitized.append(
                {
                    "type": "path",
                    "d": d,
                    "fill": _sanitize_color(item.get("fill"), default="none"),
                    "stroke": _sanitize_color(item.get("stroke"), default="#38bdf8"),
                    "width": width,
                }
            )
        elif stroke_type == "circle":
            cx = _clamp_number(item.get("cx"), 0, CANVAS_WIDTH)
            cy = _clamp_number(item.get("cy"), 0, CANVAS_HEIGHT)
            r = _clamp_number(item.get("r"), 0.5, 200)
            if cx is None or cy is None or r is None:
                continue
            entry: dict[str, Any] = {
                "type": "circle",
                "cx": cx,
                "cy": cy,
                "r": r,
                "fill": _sanitize_color(item.get("fill"), default="#38bdf8"),
            }
            if "stroke" in item and item["stroke"] is not None:
                entry["stroke"] = _sanitize_color(item.get("stroke"), default="none")
            if "width" in item and item["width"] is not None:
                entry["width"] = _clamp_number(item.get("width"), 0.5, 24, default=2)
            sanitized.append(entry)
        elif stroke_type == "ellipse":
            cx = _clamp_number(item.get("cx"), 0, CANVAS_WIDTH)
            cy = _clamp_number(item.get("cy"), 0, CANVAS_HEIGHT)
            rx = _clamp_number(item.get("rx"), 0.5, 200)
            ry = _clamp_number(item.get("ry"), 0.5, 150)
            if cx is None or cy is None or rx is None or ry is None:
                continue
            entry = {
                "type": "ellipse",
                "cx": cx,
                "cy": cy,
                "rx": rx,
                "ry": ry,
                "fill": _sanitize_color(item.get("fill"), default="#38bdf8"),
            }
            if "stroke" in item and item["stroke"] is not None:
                entry["stroke"] = _sanitize_color(item.get("stroke"), default="none")
            if "width" in item and item["width"] is not None:
                entry["width"] = _clamp_number(item.get("width"), 0.5, 24, default=2)
            sanitized.append(entry)
        elif stroke_type == "rect":
            x = _clamp_number(item.get("x"), 0, CANVAS_WIDTH)
            y = _clamp_number(item.get("y"), 0, CANVAS_HEIGHT)
            width = _clamp_number(item.get("width"), 0.5, CANVAS_WIDTH)
            height = _clamp_number(item.get("height"), 0.5, CANVAS_HEIGHT)
            if x is None or y is None or width is None or height is None:
                continue
            rx = _clamp_number(item.get("rx", 0), 0, 200, default=0)
            entry = {
                "type": "rect",
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "rx": rx,
                "fill": _sanitize_color(item.get("fill"), default="#38bdf8"),
                "stroke": _sanitize_color(item.get("stroke"), default="#0284c7"),
            }
            stroke_w = item.get("stroke_width", item.get("strokeWidth"))
            if stroke_w is not None:
                entry["stroke_width"] = _clamp_number(stroke_w, 0.5, 24, default=2)
            sanitized.append(entry)
        elif stroke_type == "line":
            x1 = _clamp_number(item.get("x1"), 0, CANVAS_WIDTH)
            y1 = _clamp_number(item.get("y1"), 0, CANVAS_HEIGHT)
            x2 = _clamp_number(item.get("x2"), 0, CANVAS_WIDTH)
            y2 = _clamp_number(item.get("y2"), 0, CANVAS_HEIGHT)
            if x1 is None or y1 is None or x2 is None or y2 is None:
                continue
            width = _clamp_number(item.get("width", 4), 0.5, 24, default=4)
            sanitized.append(
                {
                    "type": "line",
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "stroke": _sanitize_color(item.get("stroke"), default="#38bdf8"),
                    "width": width,
                }
            )
        elif stroke_type == "polygon":
            points = _sanitize_polygon_points(item.get("points"))
            if not points:
                continue
            width = _clamp_number(item.get("width", 3), 0.5, 24, default=3)
            sanitized.append(
                {
                    "type": "polygon",
                    "points": points,
                    "fill": _sanitize_color(item.get("fill"), default="#38bdf8"),
                    "stroke": _sanitize_color(item.get("stroke"), default="#0284c7"),
                    "width": width,
                }
            )
    return sanitized


_LLM_ROUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "word": {"type": "string", "minLength": 1, "maxLength": 24},
        "category": {"type": "string", "minLength": 1, "maxLength": 24},
        "aliases": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 24},
            "minItems": 1,
            "maxItems": 8,
        },
        "progressive_hints": {
            "type": "array",
            "items": {"type": "string", "minLength": 2, "maxLength": 80},
            "minItems": 3,
            "maxItems": 3,
        },
        "strokes": {
            "type": "array",
            "items": {"type": "object"},
            "minItems": 5,
            "maxItems": 14,
        },
    },
    "required": ["word", "category", "aliases", "progressive_hints", "strokes"],
    "additionalProperties": False,
}

_LLM_EVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "won": {"type": "boolean"},
        "near_miss": {"type": "boolean"},
        "roast": {"type": "string", "maxLength": 120},
    },
    "required": ["won", "near_miss", "roast"],
    "additionalProperties": False,
}


def normalize_guess(text: str) -> str:
    """Normalize input text for cheap and fast regex/string matching."""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    text = re.sub(r"^(?:我猜(?:是)?|答案是|这就是|这是|大概是|难道是|会不会是|是|是不是)\s*", "", text)
    return "".join(char for char in text if char.isalnum())


class DrawAndGuessService:
    def __init__(self, daily_memory=None):
        self.daily_memory = daily_memory
        self.state: Literal["idle", "drawing", "finished"] = "idle"
        self.current_word: dict[str, Any] | None = None
        self.started_at: float = 0.0
        self.duration_sec: int = 60
        self.trigger_user: str = "房管"
        self.gift_name: str | None = None
        self.winner: str | None = None
        self.winner_user_id: str | None = None
        self.last_finish_at: float = 0.0
        self.recent_words: list[str] = []
        self.mode: str = "aqua_draws"
        self.theme: str | None = None
        self.llm_generated: bool = False
        self.scoreboard: dict[str, int] = {}

    def is_active(self) -> bool:
        return self.state == "drawing" and (time.time() - self.started_at < self.duration_sec)

    def _select_fallback_word(self, theme: str | None = None) -> dict[str, Any]:
        recent_tail = {w.casefold() for w in self.recent_words[-5:]}
        pool = [
            w for w in WORD_BANK
            if w["id"].casefold() not in recent_tail and w["word"].casefold() not in recent_tail
        ] or WORD_BANK

        if theme and theme.strip():
            needle = theme.strip().casefold()
            themed = [
                w for w in pool
                if needle in w["category"].casefold()
                or needle in w["word"].casefold()
                or needle in w["id"].casefold()
                or any(needle in alias.casefold() for alias in w.get("aliases", []))
            ]
            if themed:
                pool = themed
        return random.choice(pool)

    def _begin_round(
        self,
        chosen: dict[str, Any],
        *,
        trigger_user: str,
        gift_name: str | None,
        duration_sec: int,
        theme: str | None,
        mode: str,
        llm_generated: bool,
    ) -> dict[str, Any]:
        clean_strokes = sanitize_strokes(chosen.get("strokes", []))
        normalized_word = {
            **chosen,
            "strokes": clean_strokes,
            "progressive_hints": list(chosen.get("progressive_hints") or []),
            "hint": chosen.get("hint") or (chosen.get("progressive_hints") or [""])[0],
        }
        recent_key = chosen["word"] if llm_generated else chosen["id"]
        self.recent_words.append(recent_key)

        self.current_word = normalized_word
        self.state = "drawing"
        self.started_at = time.time()
        self.duration_sec = duration_sec
        self.trigger_user = trigger_user
        self.gift_name = gift_name
        self.winner = None
        self.winner_user_id = None
        self.mode = mode
        self.theme = theme
        self.llm_generated = llm_generated

        return {
            "status": "started",
            "word_id": normalized_word["id"],
            "category": normalized_word["category"],
            "trigger_user": trigger_user,
            "gift_name": gift_name,
            "spoken_text": f"{trigger_user}开启了你画我猜，本轮分类「{normalized_word['category']}」，弹幕赶紧抢答，别猜太离谱哦！",
            "duration_sec": duration_sec,
            "mode": mode,
            "theme": theme,
            "llm_generated": llm_generated,
        }

    def start_round(
        self,
        trigger_user: str = "观众",
        gift_name: str | None = None,
        duration_sec: int = 60,
        theme: str | None = None,
        mode: str = "aqua_draws",
    ) -> dict[str, Any]:
        """Starts a new round of Draw & Guess using the built-in WORD_BANK."""
        chosen = self._select_fallback_word(theme=theme)
        return self._begin_round(
            chosen,
            trigger_user=trigger_user,
            gift_name=gift_name,
            duration_sec=duration_sec,
            theme=theme,
            mode=mode,
            llm_generated=False,
        )

    async def start_round_llm(
        self,
        llm,
        *,
        trigger_user: str = "观众",
        theme: str | None = None,
        gift_name: str | None = None,
        duration_sec: int = 75,
        mode: str = "aqua_draws",
    ) -> dict[str, Any]:
        """Start a round using LLM-generated word, hints, and safe SVG strokes; fall back to WORD_BANK on error."""
        if llm is None or not callable(getattr(llm, "generate_json", None)):
            return self.start_round(
                trigger_user=trigger_user,
                gift_name=gift_name,
                duration_sec=duration_sec,
                theme=theme,
                mode=mode,
            )

        avoid_words = self.recent_words[-10:]
        theme_line = f"指定主题/风格：{theme}" if theme else "自由发挥（科技、美食、生活、二次元或梗图皆可）"
        avoid_line = f"必须避开近期题目：{', '.join(avoid_words)}" if avoid_words else "暂无历史避开词。"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是直播间的蓝发小鲸鱼主播 Aqua（傲娇毒舌但不说脏话，严禁自称“大肥鱼”）。"
                    "现在为“你画我猜”生成一题，并给出可在 400x300 SVG 画布渲染的矢量笔画。\n"
                    "规则：\n"
                    "1. word 为简洁明确的中文或常见英文谜底；aliases 给 2~6 个同义词/简称。\n"
                    "2. progressive_hints 必须恰好 3 条，由浅入深，严禁在提示里直接出现谜底原文。\n"
                    "3. strokes 给 5~14 个安全图元，仅限 type 为 path/circle/ellipse/rect/line/polygon，"
                    "坐标严格在 x:[0,400], y:[0,300] 内，颜色只能用 hex（如 #38bdf8）或 none。"
                ),
            },
            {
                "role": "user",
                "content": f"触发观众：{trigger_user}\n模式：{mode}\n{theme_line}\n{avoid_line}\n请输出 JSON。",
            },
        ]

        try:
            raw = await llm.generate_json(messages, _LLM_ROUND_SCHEMA, max_tokens=1400)
            parsed = self._parse_llm_round(raw)
            if parsed is not None:
                return self._begin_round(
                    parsed,
                    trigger_user=trigger_user,
                    gift_name=gift_name,
                    duration_sec=duration_sec,
                    theme=theme,
                    mode=mode,
                    llm_generated=True,
                )
        except Exception:
            pass

        return self.start_round(
            trigger_user=trigger_user,
            gift_name=gift_name,
            duration_sec=duration_sec,
            theme=theme,
            mode=mode,
        )

    def _parse_llm_round(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        word = str(raw.get("word") or "").strip()
        if not word or len(word) > 24:
            return None
        recent_norm = {normalize_guess(w) for w in self.recent_words[-10:] if w}
        if normalize_guess(word) in recent_norm:
            return None

        category = str(raw.get("category") or "脑洞出题").strip()[:24] or "脑洞出题"
        raw_aliases = raw.get("aliases")
        aliases: list[str] = []
        if isinstance(raw_aliases, list):
            for item in raw_aliases:
                val = str(item or "").strip()
                if val and val not in aliases:
                    aliases.append(val)
        if word not in aliases:
            aliases.insert(0, word)

        strokes = sanitize_strokes(raw.get("strokes"))
        if not strokes:
            return None

        raw_hints = raw.get("progressive_hints")
        hints: list[str] = []
        if isinstance(raw_hints, list):
            hints = [str(h or "").strip() for h in raw_hints if str(h or "").strip()]
        if len(hints) < 3:
            fallback_hints = [
                f"分类提示：{category}",
                f"一共有 {len(word)} 个字（或字符），再仔细看看画板轮廓",
                f"线索集中在「{category}」这个方向，就差临门一脚啦",
            ]
            while len(hints) < 3:
                hints.append(fallback_hints[len(hints)])
        hints = hints[:3]

        return {
            "id": f"llm_{int(time.time() * 1000)}",
            "word": word,
            "category": category,
            "aliases": aliases,
            "hint": hints[1],
            "progressive_hints": hints,
            "strokes": strokes,
        }

    def _finalize_win(
        self,
        *,
        user_id: str | None,
        display_name: str,
        text: str,
        character_id: str | None,
        character_version: int,
        roast: str | None = None,
        semantic_match: bool = False,
    ) -> dict[str, Any]:
        assert self.current_word is not None
        self.state = "finished"
        self.winner = display_name
        self.winner_user_id = user_id
        self.last_finish_at = time.time()
        self.scoreboard[display_name] = self.scoreboard.get(display_name, 0) + 1
        word = self.current_word["word"]

        if character_id and self.daily_memory and hasattr(self.daily_memory, "record_highlight"):
            try:
                today_str = datetime.now(UTC).strftime("%Y-%m-%d")
                self.daily_memory.record_highlight(
                    character_id=character_id,
                    character_version=character_version,
                    highlight_date=today_str,
                    category="live_highlight",
                    headline=f"观众 {display_name} 猜中了你画我猜题目【{word}】",
                    quote=f"{display_name}: {text}",
                    anecdote=f"【你画我猜】的答案是【{word}】，观众【{display_name}】第一个猜中。",
                    source_ref=f"draw_guess_{self.current_word['id']}",
                    source_user=display_name,
                    tags=["你画我猜", "互动", word],
                    source_evidence={
                        "version": 1,
                        "kind": "runtime_event",
                        "confirmed_by": "runtime_event",
                        "source_ref": f"draw_guess_{self.current_word['id']}",
                        "quote": f"{display_name}: {text}",
                        "round_id": f"{self.current_word['id']}:{self.started_at}",
                        "round_started_at": self.started_at,
                        "winner_user_id": user_id,
                        "guess": text,
                        "answer": word,
                    },
                )
            except Exception:
                import traceback

                traceback.print_exc()

        payload: dict[str, Any] = {
            "status": "won",
            "won": True,
            "winner": display_name,
            "display_name": display_name,
            "word": word,
            "elapsed_sec": round(time.time() - self.started_at, 1),
            "scoreboard": dict(self.scoreboard),
        }
        if roast:
            payload["roast"] = roast
        if semantic_match:
            payload["semantic_match"] = True
        return payload

    def check_guess(
        self,
        user_id: str | None,
        display_name: str,
        text: str,
        is_gift: bool = False,
        coin: int = 0,
        character_id: str | None = "whale",
        character_version: int = 1,
    ) -> dict[str, Any] | None:
        """Fast regex/string match for incoming danmaku or gift guess."""
        del is_gift, coin
        if not self.is_active() or not self.current_word:
            return None

        norm = normalize_guess(text)
        if not norm:
            return None

        target = normalize_guess(self.current_word["word"])
        aliases = [normalize_guess(a) for a in self.current_word.get("aliases", []) if normalize_guess(a)]

        is_match = (norm == target) or any(norm == a for a in aliases)
        if not is_match:
            is_match = (bool(target) and target in norm) or any(len(a) >= 2 and a in norm for a in aliases)

        if not is_match:
            return None

        return self._finalize_win(
            user_id=user_id,
            display_name=display_name,
            text=text,
            character_id=character_id,
            character_version=character_version,
        )

    async def evaluate_guess_llm(
        self,
        llm,
        *,
        user_id: str | None,
        display_name: str,
        text: str,
        character_id: str | None = "whale",
        character_version: int = 1,
    ) -> dict[str, Any] | None:
        """Fast-path check_guess first; otherwise ask LLM for semantic match or near-miss Aqua roast."""
        if not self.is_active() or not self.current_word:
            return None

        fast_win = self.check_guess(
            user_id=user_id,
            display_name=display_name,
            text=text,
            character_id=character_id,
            character_version=character_version,
        )
        if fast_win is not None:
            return fast_win

        if not normalize_guess(text):
            return None
        if llm is None or not callable(getattr(llm, "generate_json", None)):
            return None

        word = self.current_word["word"]
        category = self.current_word.get("category", "")
        aliases = self.current_word.get("aliases", [])
        messages = [
            {
                "role": "system",
                "content": (
                    "你是蓝发小鲸鱼主播 Aqua，正在主持“你画我猜”。"
                    "语气保持机敏、傲娇、幽默但不说脏话。\n"
                    "判定观众猜测：\n"
                    "1. 如果语义上基本等价（例如同义表达、只差无伤大雅的修饰或常见错别字），won=true。\n"
                    "2. 如果没猜中但方向很接近或特别离谱搞笑（near_miss=true），用 Aqua 口吻给一句尖锐不失礼貌的吐槽提示（roast），"
                    "严禁在 roast 里直接剧透或说出谜底原文！\n"
                    "3. 普通无关闲聊则 won=false, near_miss=false, roast=''。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "word": word,
                        "category": category,
                        "aliases": aliases,
                        "viewer": display_name,
                        "guess": text,
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        try:
            res = await llm.generate_json(messages, _LLM_EVAL_SCHEMA, max_tokens=256)
        except Exception:
            return None

        if not isinstance(res, dict):
            return None

        roast = str(res.get("roast") or "").strip()
        if res.get("won") is True:
            if not self.is_active() or not self.current_word:
                return None
            return self._finalize_win(
                user_id=user_id,
                display_name=display_name,
                text=text,
                character_id=character_id,
                character_version=character_version,
                roast=roast or None,
                semantic_match=True,
            )

        if res.get("near_miss") is True:
            if word and word.casefold() in roast.casefold():
                roast = re.sub(re.escape(word), "***", roast, flags=re.IGNORECASE)
            if not roast:
                roast = "哼，方向算你蹭到一点边了，再仔细盯紧画板细节猜一次！"
            return {
                "won": False,
                "near_miss": True,
                "roast": roast,
                "display_name": display_name,
            }

        return None

    def tick(self) -> dict[str, Any] | None:
        """Advance timer and check timeout."""
        if self.state != "drawing" or not self.current_word:
            return None

        elapsed = time.time() - self.started_at
        if elapsed >= self.duration_sec:
            self.state = "finished"
            self.winner = None
            self.last_finish_at = time.time()
            word = self.current_word["word"]
            return {
                "status": "timeout",
                "word": word,
            }
        return None

    def stop_round(self, reason: str = "主播手动停止") -> dict[str, Any]:
        self.state = "finished"
        self.last_finish_at = time.time()
        word = self.current_word["word"] if self.current_word else "未知"
        return {"status": "stopped", "word": word, "reason": reason}

    def get_overlay_state(self) -> dict[str, Any]:
        """Provides real-time state for OBS overlay and Web UI."""
        if self.state == "drawing":
            self.tick()

        elapsed = time.time() - self.started_at if self.state != "idle" else 0.0
        total_duration = self.duration_sec
        remaining = max(0.0, total_duration - elapsed) if self.state == "drawing" else 0.0

        if not self.current_word:
            return {
                "state": self.state,
                "active": False,
                "word": "",
                "category": "",
                "hint": "",
                "progressive_hint": "",
                "hint_level": 0,
                "revealed_strokes": [],
                "total_strokes": 0,
                "elapsed": 0,
                "remaining": 0,
                "winner": None,
                "trigger_user": self.trigger_user,
                "mode": self.mode,
                "theme": self.theme,
                "scoreboard": dict(self.scoreboard),
                "llm_generated": self.llm_generated,
            }

        strokes = sanitize_strokes(self.current_word.get("strokes", []))
        total_strokes = len(strokes)

        progress = min(1.0, elapsed / max(1.0, total_duration * 0.85))
        revealed_count = (
            max(1, min(total_strokes, int(progress * total_strokes)))
            if self.state == "drawing" and total_strokes > 0
            else total_strokes
        )
        revealed_strokes = strokes[:revealed_count]

        ratio = elapsed / max(1.0, float(total_duration))
        if self.state == "finished" or ratio >= 0.70:
            hint_level = 3
        elif ratio >= 0.35:
            hint_level = 2
        else:
            hint_level = 1

        progressive_hints = self.current_word.get("progressive_hints") or []
        legacy_hint = self.current_word.get("hint", "")
        if progressive_hints:
            progressive_hint = str(progressive_hints[min(hint_level - 1, len(progressive_hints) - 1)])
            hint = progressive_hint
        else:
            progressive_hint = legacy_hint
            hint = legacy_hint if (elapsed > total_duration * 0.45 or self.state == "finished") else ""

        return {
            "state": self.state,
            "active": self.state == "drawing",
            "word": self.current_word["word"] if self.state == "finished" else "???",
            "category": self.current_word["category"],
            "hint": hint,
            "progressive_hint": progressive_hint,
            "hint_level": hint_level,
            "revealed_strokes": revealed_strokes,
            "total_strokes": total_strokes,
            "elapsed": round(elapsed, 1),
            "remaining": round(remaining, 1),
            "winner": self.winner,
            "trigger_user": self.trigger_user,
            "mode": self.mode,
            "theme": self.theme,
            "scoreboard": dict(self.scoreboard),
            "llm_generated": self.llm_generated,
        }
