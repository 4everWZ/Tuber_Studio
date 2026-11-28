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


WORD_BANK: list[dict[str, Any]] = [
    {
        "id": "aqua",
        "word": "Aqua",
        "category": "蓝发小鲸鱼",
        "aliases": ["aqua", "小鲸鱼", "蓝发小鲸鱼", "鲸鱼", "whale"],
        "hint": "本直播间的蓝发小鲸鱼主播本尊，尾巴会喷水花！",
        "progressive_hints": [
            "海洋主题的二次元角色，标志配色是天蓝色",
            "头顶有小水花和蓝发，而且就是正在画画的这位主播",
            "四个英文字母的偶像名字，大家也常叫她小鲸鱼",
        ],
        "strokes": [
            {
                "type": "path",
                "d": "M 65 160 C 65 85, 225 75, 265 140 C 305 118, 340 92, 350 128 C 332 150, 325 168, 350 192 C 320 192, 275 170, 245 176 C 180 220, 82 212, 65 160 Z",
                "fill": "#38bdf8",
                "stroke": "#0284c7",
                "width": 5,
            },
            {"type": "circle", "cx": 115, "cy": 140, "r": 8, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 122, "cy": 156, "rx": 10, "ry": 5, "fill": "#fda4af"},
            {
                "type": "path",
                "d": "M 92 168 Q 120 186, 148 168",
                "fill": "none",
                "stroke": "#0f172a",
                "width": 4,
            },
            {
                "type": "path",
                "d": "M 188 94 Q 178 58, 156 68 M 188 94 Q 198 58, 220 68 M 188 94 L 188 60",
                "fill": "none",
                "stroke": "#bae6fd",
                "width": 4,
            },
        ],
    },
    {
        "id": "mechanical_keyboard",
        "word": "机械键盘",
        "category": "数码外设",
        "aliases": ["机械键盘", "键盘", "客制化键盘", "keyboard"],
        "hint": "啪嗒啪嗒打字超带感，一排排方块键帽还会发光！",
        "progressive_hints": [
            "桌面常见数码外设，需要两只手配合敲击",
            "里面有很多个独立轴体，敲起来会有清脆的咔嗒声",
            "四个字，程序员和打字狂魔最爱的输入设备",
        ],
        "strokes": [
            {"type": "rect", "x": 45, "y": 95, "width": 310, "height": 120, "rx": 12, "fill": "#1e293b", "stroke": "#38bdf8"},
            {"type": "rect", "x": 65, "y": 115, "width": 36, "height": 26, "rx": 4, "fill": "#38bdf8", "stroke": "#0284c7"},
            {"type": "rect", "x": 110, "y": 115, "width": 36, "height": 26, "rx": 4, "fill": "#e2e8f0", "stroke": "#94a3b8"},
            {"type": "rect", "x": 155, "y": 115, "width": 36, "height": 26, "rx": 4, "fill": "#e2e8f0", "stroke": "#94a3b8"},
            {"type": "rect", "x": 200, "y": 115, "width": 36, "height": 26, "rx": 4, "fill": "#e2e8f0", "stroke": "#94a3b8"},
            {"type": "rect", "x": 245, "y": 115, "width": 90, "height": 26, "rx": 4, "fill": "#f472b6", "stroke": "#db2777"},
            {"type": "rect", "x": 110, "y": 165, "width": 170, "height": 26, "rx": 4, "fill": "#cbd5e1", "stroke": "#64748b"},
        ],
    },
    {
        "id": "hot_white_rice",
        "word": "热白饭",
        "category": "干饭日常",
        "aliases": ["热白饭", "白饭", "米饭", "白米饭", "大米饭", "rice"],
        "hint": "冒着热气的雪白主食，下播第一件事就是炫一碗！",
        "progressive_hints": [
            "日常饮食里最基础的主食，通常装在圆碗里",
            "刚出锅时会冒出弯弯的热气，配啥菜都香",
            "Aqua 每次下播都念叨要吃的那碗热腾腾的白米主食",
        ],
        "strokes": [
            {"type": "path", "d": "M 105 160 Q 200 95, 295 160 Z", "fill": "#f8fafc", "stroke": "#cbd5e1", "width": 4},
            {"type": "path", "d": "M 95 160 C 100 245, 300 245, 305 160 Z", "fill": "#38bdf8", "stroke": "#0284c7", "width": 5},
            {"type": "rect", "x": 150, "y": 225, "width": 100, "height": 18, "rx": 4, "fill": "#0284c7", "stroke": "#0369a1"},
            {"type": "path", "d": "M 160 110 Q 150 85, 165 60", "fill": "none", "stroke": "#94a3b8", "width": 4},
            {"type": "path", "d": "M 200 102 Q 190 75, 205 50", "fill": "none", "stroke": "#94a3b8", "width": 4},
            {"type": "path", "d": "M 240 110 Q 230 85, 245 60", "fill": "none", "stroke": "#94a3b8", "width": 4},
        ],
    },
    {
        "id": "graphics_card",
        "word": "显卡",
        "category": "硬核硬件",
        "aliases": ["显卡", "gpu", "独立显卡", "显示卡"],
        "hint": "机箱里带双风扇的昂贵砖头，没有它游戏和模型都跑不动！",
        "progressive_hints": [
            "电脑主机箱里面的核心矩形硬件，价格经常很贵",
            "正面通常有两个或三个散热风扇，插在主板上",
            "两个字，玩3A大作和跑本地模型全靠它算力输出",
        ],
        "strokes": [
            {"type": "rect", "x": 65, "y": 95, "width": 270, "height": 115, "rx": 10, "fill": "#334155", "stroke": "#0f172a"},
            {"type": "circle", "cx": 145, "cy": 152, "r": 40, "fill": "#1e293b", "stroke": "#38bdf8", "width": 4},
            {"type": "circle", "cx": 255, "cy": 152, "r": 40, "fill": "#1e293b", "stroke": "#38bdf8", "width": 4},
            {"type": "circle", "cx": 145, "cy": 152, "r": 12, "fill": "#38bdf8"},
            {"type": "circle", "cx": 255, "cy": 152, "r": 12, "fill": "#38bdf8"},
            {"type": "rect", "x": 110, "y": 210, "width": 140, "height": 16, "rx": 2, "fill": "#facc15", "stroke": "#ca8a04"},
        ],
    },
    {
        "id": "bubble_tea",
        "word": "珍珠奶茶",
        "category": "快乐饮品",
        "aliases": ["珍珠奶茶", "奶茶", "波霸奶茶", "boba"],
        "hint": "插着粗吸管的冰甜杯子，杯底全是圆溜溜的黑珍珠！",
        "progressive_hints": [
            "年轻人最爱的街边手摇甜饮，装在透明塑料杯里",
            "杯子顶上插着一根很粗的吸管，喝起来有嚼劲",
            "四个字，里面有奶有茶，底部沉着一排黑色小圆球",
        ],
        "strokes": [
            {"type": "polygon", "points": "140,95 260,95 240,255 160,255", "fill": "#fde68a", "stroke": "#d97706", "width": 4},
            {"type": "line", "x1": 130, "y1": 95, "x2": 270, "y2": 95, "stroke": "#0f172a", "width": 6},
            {"type": "line", "x1": 210, "y1": 35, "x2": 195, "y2": 215, "stroke": "#ec4899", "width": 8},
            {"type": "circle", "cx": 175, "cy": 235, "r": 9, "fill": "#1e293b"},
            {"type": "circle", "cx": 200, "cy": 238, "r": 9, "fill": "#1e293b"},
            {"type": "circle", "cx": 225, "cy": 235, "r": 9, "fill": "#1e293b"},
            {"type": "circle", "cx": 188, "cy": 218, "r": 9, "fill": "#1e293b"},
        ],
    },
    {
        "id": "mini_fan",
        "word": "小电风扇",
        "category": "消暑好物",
        "aliases": ["小电风扇", "电风扇", "风扇", "小风扇", "fan"],
        "hint": "夏天桌面上呼呼吹风的小家电，网罩里面有三片扇叶！",
        "progressive_hints": [
            "夏天离不开的桌面小电器，圆脑袋下面有底座",
            "网罩里面有旋转的扇叶，开起来会呼呼吹凉风",
            "四个字，比空调迷你，放电脑桌边刚刚好",
        ],
        "strokes": [
            {"type": "circle", "cx": 200, "cy": 135, "r": 75, "fill": "#e0f2fe", "stroke": "#0284c7", "width": 5},
            {"type": "ellipse", "cx": 200, "cy": 95, "rx": 14, "ry": 32, "fill": "#38bdf8", "stroke": "#0284c7", "width": 3},
            {"type": "ellipse", "cx": 168, "cy": 155, "rx": 28, "ry": 14, "fill": "#38bdf8", "stroke": "#0284c7", "width": 3},
            {"type": "ellipse", "cx": 232, "cy": 155, "rx": 28, "ry": 14, "fill": "#38bdf8", "stroke": "#0284c7", "width": 3},
            {"type": "circle", "cx": 200, "cy": 135, "r": 14, "fill": "#0f172a"},
            {"type": "line", "x1": 200, "y1": 210, "x2": 200, "y2": 255, "stroke": "#475569", "width": 8},
            {"type": "rect", "x": 145, "y": 252, "width": 110, "height": 22, "rx": 8, "fill": "#38bdf8", "stroke": "#0284c7"},
        ],
    },
    {
        "id": "headphones",
        "word": "耳机",
        "category": "数码外设",
        "aliases": ["耳机", "头戴式耳机", "耳麦", "headphones", "headset"],
        "hint": "弯弯头梁加上一对软耳罩，挂头上就能隔绝世界听歌！",
        "progressive_hints": [
            "音频配件，左右对称戴在脑袋两侧",
            "上面有一个半圆弧形头梁，底下有两个厚耳罩",
            "两个字，听歌、打游戏、当主播监听都离不开它",
        ],
        "strokes": [
            {"type": "path", "d": "M 105 170 C 105 60, 295 60, 295 170", "fill": "none", "stroke": "#0f172a", "width": 10},
            {"type": "rect", "x": 82, "y": 145, "width": 36, "height": 78, "rx": 16, "fill": "#38bdf8", "stroke": "#0284c7"},
            {"type": "rect", "x": 282, "y": 145, "width": 36, "height": 78, "rx": 16, "fill": "#38bdf8", "stroke": "#0284c7"},
            {"type": "rect", "x": 112, "y": 158, "width": 14, "height": 52, "rx": 6, "fill": "#1e293b", "stroke": "#0f172a"},
            {"type": "rect", "x": 274, "y": 158, "width": 14, "height": 52, "rx": 6, "fill": "#1e293b", "stroke": "#0f172a"},
        ],
    },
    {
        "id": "cat",
        "word": "猫咪",
        "category": "萌宠动物",
        "aliases": ["猫", "小猫", "猫咪", "咪咪", "猫猫", "cat"],
        "hint": "三角形的耳朵，长长的胡须，高冷起来像小主子！",
        "progressive_hints": [
            "毛茸茸的常见宠物，有一对尖尖的三角形耳朵",
            "脸颊两边各长着两撇长胡须，粉嫩的小鼻子",
            "两个字，最爱踩奶和喵喵叫的傲娇小动物",
        ],
        "strokes": [
            {"type": "circle", "cx": 200, "cy": 175, "r": 65, "fill": "#fef08a", "stroke": "#ca8a04", "width": 5},
            {"type": "polygon", "points": "142,136 132,78 176,118", "fill": "#fde047", "stroke": "#ca8a04", "width": 5},
            {"type": "polygon", "points": "258,136 268,78 224,118", "fill": "#fde047", "stroke": "#ca8a04", "width": 5},
            {"type": "circle", "cx": 176, "cy": 166, "r": 8, "fill": "#0f172a"},
            {"type": "circle", "cx": 224, "cy": 166, "r": 8, "fill": "#0f172a"},
            {"type": "polygon", "points": "194,184 206,184 200,192", "fill": "#f43f5e", "stroke": "#f43f5e", "width": 2},
            {"type": "line", "x1": 142, "y1": 176, "x2": 90, "y2": 170, "stroke": "#0f172a", "width": 3},
            {"type": "line", "x1": 258, "y1": 176, "x2": 310, "y2": 170, "stroke": "#0f172a", "width": 3},
        ],
    },
    {
        "id": "watermelon",
        "word": "西瓜",
        "category": "水果美食",
        "aliases": ["西瓜", "大西瓜", "watermelon"],
        "hint": "夏天必备消暑神器，绿色的外皮，红色的果肉，带黑色小籽！",
        "progressive_hints": [
            "夏天切开吃的水果，这一画是一半月牙形状",
            "外圈是深绿色果皮，里面是大片鲜红果肉",
            "两个字，红瓤黑籽的消暑大圆瓜",
        ],
        "strokes": [
            {"type": "path", "d": "M 80 120 A 120 120 0 0 0 320 120 Z", "fill": "#22c55e", "stroke": "#15803d", "width": 6},
            {"type": "path", "d": "M 95 120 A 105 105 0 0 0 305 120 Z", "fill": "#ef4444", "stroke": "#b91c1c", "width": 3},
            {"type": "ellipse", "cx": 150, "cy": 150, "rx": 4, "ry": 7, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 200, "cy": 172, "rx": 4, "ry": 7, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 250, "cy": 150, "rx": 4, "ry": 7, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 175, "cy": 195, "rx": 4, "ry": 7, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 225, "cy": 195, "rx": 4, "ry": 7, "fill": "#0f172a"},
        ],
    },
    {
        "id": "sunflower",
        "word": "向日葵",
        "category": "植物花卉",
        "aliases": ["向日葵", "太阳花", "葵花", "sunflower"],
        "hint": "总是面朝着太阳微笑，金黄色的花瓣，中间是大圆花盘！",
        "progressive_hints": [
            "一种高高的开花植物，绿色茎干上长着叶子",
            "金黄色大花圈中间有个深棕色圆盘，能结瓜子",
            "三个字，名字里带太阳的朝阳性花朵",
        ],
        "strokes": [
            {"type": "line", "x1": 200, "y1": 180, "x2": 200, "y2": 285, "stroke": "#16a34a", "width": 8},
            {"type": "path", "d": "M 200 240 Q 245 220, 265 240 Q 235 262, 200 250", "fill": "#22c55e", "stroke": "#16a34a", "width": 3},
            {"type": "circle", "cx": 200, "cy": 125, "r": 72, "fill": "#facc15", "stroke": "#ca8a04", "width": 4},
            {"type": "circle", "cx": 200, "cy": 125, "r": 44, "fill": "#78350f", "stroke": "#451a03", "width": 4},
            {"type": "circle", "cx": 186, "cy": 115, "r": 5, "fill": "#fde047"},
            {"type": "circle", "cx": 214, "cy": 115, "r": 5, "fill": "#fde047"},
            {"type": "path", "d": "M 185 138 Q 200 152, 215 138", "fill": "none", "stroke": "#fde047", "width": 3},
        ],
    },
    {
        "id": "snowman",
        "word": "雪人",
        "category": "冬日趣物",
        "aliases": ["雪人", "雪宝", "snowman"],
        "hint": "冬天用两个大雪球堆起来的，胡萝卜当鼻子！",
        "progressive_hints": [
            "冬天限定手工作品，由一大一小两个白球叠在一起",
            "中间插着根橙色尖鼻子，两边还插了树枝当手臂",
            "两个字，天气一暖和就会融化的小胖胖雕塑",
        ],
        "strokes": [
            {"type": "circle", "cx": 200, "cy": 215, "r": 62, "fill": "#f8fafc", "stroke": "#94a3b8", "width": 5},
            {"type": "circle", "cx": 200, "cy": 120, "r": 42, "fill": "#f8fafc", "stroke": "#94a3b8", "width": 5},
            {"type": "circle", "cx": 186, "cy": 112, "r": 5, "fill": "#0f172a"},
            {"type": "circle", "cx": 214, "cy": 112, "r": 5, "fill": "#0f172a"},
            {"type": "polygon", "points": "200,122 234,126 200,132", "fill": "#f97316", "stroke": "#ea580c", "width": 2},
            {"type": "line", "x1": 152, "y1": 192, "x2": 102, "y2": 158, "stroke": "#78350f", "width": 5},
            {"type": "line", "x1": 248, "y1": 192, "x2": 298, "y2": 158, "stroke": "#78350f", "width": 5},
        ],
    },
    {
        "id": "car",
        "word": "小汽车",
        "category": "交通工具",
        "aliases": ["小汽车", "汽车", "车车", "轿车", "car"],
        "hint": "有四个轮子和车窗，能在公路上飞驰！",
        "progressive_hints": [
            "常见的陆地交通工具，侧面看有前后两个黑圆轮子",
            "车身是蓝色的，上方带有一块浅蓝色透明车窗",
            "三个字，马路上嘀嘀跑的代步工具",
        ],
        "strokes": [
            {
                "type": "path",
                "d": "M 80 205 L 90 155 L 150 155 L 180 105 L 260 105 L 290 155 L 320 165 L 320 205 Z",
                "fill": "#3b82f6",
                "stroke": "#1d4ed8",
                "width": 5,
            },
            {"type": "polygon", "points": "185,115 255,115 280,150 185,150", "fill": "#bae6fd", "stroke": "#0284c7", "width": 3},
            {"type": "circle", "cx": 130, "cy": 205, "r": 24, "fill": "#334155", "stroke": "#0f172a", "width": 4},
            {"type": "circle", "cx": 270, "cy": 205, "r": 24, "fill": "#334155", "stroke": "#0f172a", "width": 4},
            {"type": "circle", "cx": 130, "cy": 205, "r": 9, "fill": "#cbd5e1"},
            {"type": "circle", "cx": 270, "cy": 205, "r": 9, "fill": "#cbd5e1"},
        ],
    },
]

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
