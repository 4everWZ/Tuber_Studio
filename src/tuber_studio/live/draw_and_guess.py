"""Built-in lightweight Draw & Guess mini-game for Tuber Studio."""

import re
import time
import unicodedata
from typing import Any, Literal

WORD_BANK = [
    {
        "id": "whale",
        "word": "大肥鱼",
        "category": "主播本尊",
        "aliases": ["大肥鱼", "肥鱼", "大鲸鱼", "鲸鱼", "鱼鱼", "whale"],
        "hint": "圆滚滚的大尾巴，海洋里最可爱的蓝胖子，主播本尊！",
        "strokes": [
            {
                "type": "path",
                "d": "M 50 140 C 50 60, 230 50, 270 120 C 310 100, 350 70, 360 110 C 340 135, 330 155, 360 180 C 330 180, 280 155, 250 160 C 180 210, 70 200, 50 140 Z",
                "fill": "#38bdf8",
                "stroke": "#0284c7",
                "width": 5,
            },
            {"type": "circle", "cx": 100, "cy": 120, "r": 8, "fill": "#0f172a"},
            {
                "type": "path",
                "d": "M 80 150 Q 110 170, 140 150",
                "fill": "none",
                "stroke": "#0f172a",
                "width": 4,
            },
            {
                "type": "path",
                "d": "M 180 80 Q 200 40, 220 70",
                "fill": "#0284c7",
                "stroke": "#0284c7",
                "width": 3,
            },
            {
                "type": "path",
                "d": "M 150 170 Q 180 200, 190 170",
                "fill": "#38bdf8",
                "stroke": "#0284c7",
                "width": 4,
            },
            {
                "type": "path",
                "d": "M 200 40 Q 190 10, 170 20 M 200 40 Q 210 10, 230 20 M 200 40 L 200 15",
                "fill": "none",
                "stroke": "#bae6fd",
                "width": 4,
            },
        ],
    },
    {
        "id": "cat",
        "word": "猫咪",
        "category": "萌宠动物",
        "aliases": ["猫", "小猫", "猫咪", "咪咪", "猫猫", "cat"],
        "hint": "三角形的耳朵，长长的胡须，喜欢喵喵叫",
        "strokes": [
            {
                "type": "circle",
                "cx": 200,
                "cy": 180,
                "r": 70,
                "fill": "#fef08a",
                "stroke": "#ca8a04",
                "width": 5,
            },
            {
                "type": "path",
                "d": "M 140 140 L 130 80 L 175 120 Z",
                "fill": "#fde047",
                "stroke": "#ca8a04",
                "width": 5,
            },
            {
                "type": "path",
                "d": "M 260 140 L 270 80 L 225 120 Z",
                "fill": "#fde047",
                "stroke": "#ca8a04",
                "width": 5,
            },
            {"type": "circle", "cx": 175, "cy": 170, "r": 8, "fill": "#0f172a"},
            {"type": "circle", "cx": 225, "cy": 170, "r": 8, "fill": "#0f172a"},
            {
                "type": "path",
                "d": "M 195 188 L 205 188 L 200 195 Z",
                "fill": "#f43f5e",
                "stroke": "#f43f5e",
                "width": 2,
            },
            {
                "type": "path",
                "d": "M 140 180 L 90 175 M 140 190 L 85 195 M 260 180 L 310 175 M 260 190 L 315 195",
                "fill": "none",
                "stroke": "#0f172a",
                "width": 3,
            },
        ],
    },
    {
        "id": "watermelon",
        "word": "西瓜",
        "category": "水果美食",
        "aliases": ["西瓜", "大西瓜", "watermelon"],
        "hint": "夏天必备消暑神器，绿色的外皮，红色的果肉，带黑色小籽",
        "strokes": [
            {
                "type": "path",
                "d": "M 80 150 A 120 120 0 0 0 320 150 Z",
                "fill": "#22c55e",
                "stroke": "#15803d",
                "width": 6,
            },
            {
                "type": "path",
                "d": "M 95 150 A 105 105 0 0 0 305 150 Z",
                "fill": "#ef4444",
                "stroke": "#b91c1c",
                "width": 3,
            },
            {"type": "ellipse", "cx": 150, "cy": 175, "rx": 4, "ry": 6, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 200, "cy": 195, "rx": 4, "ry": 6, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 250, "cy": 175, "rx": 4, "ry": 6, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 175, "cy": 215, "rx": 4, "ry": 6, "fill": "#0f172a"},
            {"type": "ellipse", "cx": 225, "cy": 215, "rx": 4, "ry": 6, "fill": "#0f172a"},
        ],
    },
    {
        "id": "sunflower",
        "word": "向日葵",
        "category": "植物花卉",
        "aliases": ["向日葵", "太阳花", "葵花", "sunflower"],
        "hint": "总是面朝着太阳微笑，金黄色的花瓣，中间是大圆花盘",
        "strokes": [
            {
                "type": "path",
                "d": "M 200 240 Q 200 320 200 350",
                "fill": "none",
                "stroke": "#16a34a",
                "width": 8,
            },
            {
                "type": "path",
                "d": "M 200 290 Q 240 270 260 290 Q 230 310 200 300",
                "fill": "#22c55e",
                "stroke": "#16a34a",
                "width": 3,
            },
            {
                "type": "circle",
                "cx": 200,
                "cy": 180,
                "r": 80,
                "fill": "#facc15",
                "stroke": "#ca8a04",
                "width": 4,
            },
            {
                "type": "circle",
                "cx": 200,
                "cy": 180,
                "r": 50,
                "fill": "#78350f",
                "stroke": "#451a03",
                "width": 4,
            },
            {"type": "circle", "cx": 185, "cy": 170, "r": 5, "fill": "#fde047"},
            {"type": "circle", "cx": 215, "cy": 170, "r": 5, "fill": "#fde047"},
            {
                "type": "path",
                "d": "M 185 195 Q 200 210 215 195",
                "fill": "none",
                "stroke": "#fde047",
                "width": 3,
            },
        ],
    },
    {
        "id": "snowman",
        "word": "雪人",
        "category": "日常趣物",
        "aliases": ["雪人", "雪宝", "snowman"],
        "hint": "冬天用两个大雪球堆起来的，胡萝卜当鼻子",
        "strokes": [
            {
                "type": "circle",
                "cx": 200,
                "cy": 250,
                "r": 75,
                "fill": "#f8fafc",
                "stroke": "#94a3b8",
                "width": 5,
            },
            {
                "type": "circle",
                "cx": 200,
                "cy": 140,
                "r": 50,
                "fill": "#f8fafc",
                "stroke": "#94a3b8",
                "width": 5,
            },
            {"type": "circle", "cx": 185, "cy": 130, "r": 6, "fill": "#0f172a"},
            {"type": "circle", "cx": 215, "cy": 130, "r": 6, "fill": "#0f172a"},
            {
                "type": "path",
                "d": "M 200 142 L 235 146 L 200 152 Z",
                "fill": "#f97316",
                "stroke": "#ea580c",
                "width": 2,
            },
            {"type": "circle", "cx": 200, "cy": 220, "r": 6, "fill": "#0f172a"},
            {"type": "circle", "cx": 200, "cy": 250, "r": 6, "fill": "#0f172a"},
            {
                "type": "path",
                "d": "M 150 220 L 100 180 M 250 220 L 300 180",
                "fill": "none",
                "stroke": "#78350f",
                "width": 5,
            },
        ],
    },
    {
        "id": "car",
        "word": "小汽车",
        "category": "交通工具",
        "aliases": ["小汽车", "汽车", "车车", "轿车", "car"],
        "hint": "有四个轮子和车窗，能在公路上飞驰",
        "strokes": [
            {
                "type": "path",
                "d": "M 80 220 L 90 170 L 150 170 L 180 120 L 260 120 L 290 170 L 320 180 L 320 220 Z",
                "fill": "#3b82f6",
                "stroke": "#1d4ed8",
                "width": 5,
            },
            {
                "type": "path",
                "d": "M 185 130 L 255 130 L 280 165 L 185 165 Z",
                "fill": "#bae6fd",
                "stroke": "#0284c7",
                "width": 3,
            },
            {
                "type": "circle",
                "cx": 130,
                "cy": 220,
                "r": 25,
                "fill": "#334155",
                "stroke": "#0f172a",
                "width": 4,
            },
            {
                "type": "circle",
                "cx": 270,
                "cy": 220,
                "r": 25,
                "fill": "#334155",
                "stroke": "#0f172a",
                "width": 4,
            },
            {"type": "circle", "cx": 130, "cy": 220, "r": 10, "fill": "#cbd5e1"},
            {"type": "circle", "cx": 270, "cy": 220, "r": 10, "fill": "#cbd5e1"},
        ],
    },
]


def normalize_guess(text: str) -> str:
    """Normalize input text for cheap and fast regex/string matching."""
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    # Remove leading command phrases like "我猜是", "我猜", "答案是", "这是", "是", "是不是"
    text = re.sub(r"^(?:我猜(?:是)?|答案是|这就是|这是|大概是|难道是|会不会是|是|是不是)\s*", "", text)
    # Remove punctuation and whitespace
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

    def is_active(self) -> bool:
        return self.state == "drawing" and (time.time() - self.started_at < self.duration_sec)

    def start_round(
        self, trigger_user: str = "观众", gift_name: str | None = None, duration_sec: int = 60
    ) -> dict:
        """Starts a new round of Draw & Guess."""
        candidates = [w for w in WORD_BANK if w["id"] not in self.recent_words[-3:]] or WORD_BANK
        import random

        chosen = random.choice(candidates)
        self.recent_words.append(chosen["id"])

        self.current_word = chosen
        self.state = "drawing"
        self.started_at = time.time()
        self.duration_sec = duration_sec
        self.trigger_user = trigger_user
        self.gift_name = gift_name
        self.winner = None
        self.winner_user_id = None

        if gift_name:
            spoken_text = (
                f"当当当！大肥鱼灵魂画手上线！感谢【{trigger_user}】送出的【{gift_name}】开启你画我猜！"
                f"猜猜本鱼画的是什么？弹幕发答案抢答哦！"
            )
        else:
            spoken_text = (
                f"当当当！大肥鱼灵魂画手上线！由【{trigger_user}】发起了你画我猜！"
                f"快看看画布，猜猜本鱼画的是什么？"
            )

        return {
            "status": "started",
            "word_id": chosen["id"],
            "category": chosen["category"],
            "spoken_text": spoken_text,
            "duration_sec": duration_sec,
        }

    def check_guess(
        self,
        user_id: str | None,
        display_name: str,
        text: str,
        is_gift: bool = False,
        coin: int = 0,
        character_id: str = "whale",
        character_version: int = 1,
    ) -> dict | None:
        """
        Fast regex/string match for incoming danmaku or gift guess.
        Returns match payload if guessed correctly, otherwise None.
        """
        if not self.is_active() or not self.current_word:
            return None

        norm = normalize_guess(text)
        if not norm:
            return None

        target = normalize_guess(self.current_word["word"])
        aliases = [normalize_guess(a) for a in self.current_word.get("aliases", [])]

        is_match = (norm == target) or any(norm == a for a in aliases)
        if not is_match:
            # Check contains for phrases like "肯定是猫咪呀"
            is_match = (target in norm) or any(len(a) >= 2 and a in norm for a in aliases)

        if not is_match:
            return None

        # Win!
        self.state = "finished"
        self.winner = display_name
        self.winner_user_id = user_id
        self.last_finish_at = time.time()
        word = self.current_word["word"]

        spoken_text = (
            f"哇！恭喜【{display_name}】猜对啦！答案就是【{word}】！"
            f"太懂大肥鱼的灵魂画风了吧，必须给你比个大爱心！"
        )

        if self.daily_memory and hasattr(self.daily_memory, "record_highlight"):
            try:
                from datetime import UTC, datetime

                today_str = datetime.now(UTC).strftime("%Y-%m-%d")
                self.daily_memory.record_highlight(
                    character_id=character_id,
                    character_version=character_version,
                    highlight_date=today_str,
                    category="live_highlight",
                    headline=f"观众 {display_name} 猜中了你画我猜题目【{word}】",
                    quote=f"{display_name}: {text}",
                    anecdote=f"在直播间【你画我猜】环节中，大肥鱼手绘了【{word}】，观众【{display_name}】眼疾手快第一个猜中！",
                    source_ref=f"draw_guess_{self.current_word['id']}",
                    source_user=display_name,
                    tags=["你画我猜", "互动", word],
                )
            except Exception:
                import traceback

                traceback.print_exc()

        return {
            "status": "won",
            "winner": display_name,
            "word": word,
            "spoken_text": spoken_text,
            "elapsed_sec": round(time.time() - self.started_at, 1),
        }

    def tick(self) -> dict | None:
        """
        Advance timer and check timeout.
        Returns timeout event info if expired, else None.
        """
        if self.state != "drawing" or not self.current_word:
            return None

        elapsed = time.time() - self.started_at
        if elapsed >= self.duration_sec:
            self.state = "finished"
            self.winner = None
            self.last_finish_at = time.time()
            word = self.current_word["word"]

            spoken_text = (
                f"时间到啦！居然没人猜出来！答案其实是【{word}】呀！"
                f"本鱼画得明明这么传神，你们这群小鱼籽还得继续修炼眼力哦！"
            )
            return {
                "status": "timeout",
                "word": word,
                "spoken_text": spoken_text,
            }
        return None

    def stop_round(self, reason: str = "主播手动停止") -> dict:
        self.state = "finished"
        self.last_finish_at = time.time()
        word = self.current_word["word"] if self.current_word else "未知"
        return {"status": "stopped", "word": word, "reason": reason}

    def get_overlay_state(self) -> dict:
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
                "revealed_strokes": [],
                "total_strokes": 0,
                "elapsed": 0,
                "remaining": 0,
                "winner": None,
            }

        strokes = self.current_word.get("strokes", [])
        total_strokes = len(strokes)

        progress = min(1.0, elapsed / max(1.0, total_duration * 0.85))
        revealed_count = (
            max(1, min(total_strokes, int(progress * total_strokes)))
            if self.state == "drawing"
            else total_strokes
        )
        revealed_strokes = strokes[:revealed_count]

        category = self.current_word["category"]
        hint = (
            self.current_word["hint"] if (elapsed > total_duration * 0.45 or self.state == "finished") else ""
        )

        return {
            "state": self.state,
            "active": self.state == "drawing",
            "word": self.current_word["word"] if self.state == "finished" else "???",
            "category": category,
            "hint": hint,
            "revealed_strokes": revealed_strokes,
            "total_strokes": total_strokes,
            "elapsed": round(elapsed, 1),
            "remaining": round(remaining, 1),
            "winner": self.winner,
            "trigger_user": self.trigger_user,
        }
