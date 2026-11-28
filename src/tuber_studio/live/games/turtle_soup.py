"""Lateral-thinking (Turtle Soup / 海龟汤) mini-game service for Tuber Studio live."""

from __future__ import annotations

import random
import re
import unicodedata
from typing import Any, Literal

PUZZLE_BANK: list[dict[str, Any]] = [
    {
        "id": "midnight_elevator",
        "genre": "daily",
        "title": "只按一楼的邻居",
        "surface": "小张住在二十楼，每次出门都能直接坐电梯到一楼，但回家时如果不是雨天，他总是在十二楼下电梯改走楼梯。为什么？",
        "answer": "小张身高不够，平时只能按到十二楼的按钮；只有下雨天带着雨伞时，才能用雨伞尖按到二十楼的按钮。",
        "key_clues": [
            "小张的身高比一般成年人矮很多",
            "雨天他手里会多一件长柄工具（雨伞）",
            "问题出在够不到二十楼的电梯按钮",
        ],
        "hints": [
            "想想下雨天出门，手里通常会多出什么东西？",
            "不是电梯坏了，也不是小张故意锻炼身体，是物理够不着。",
            "跟身高还有雨伞的长度有关哦。",
        ],
        "yes_keywords": ["身高", "矮", "小孩", "小朋友", "够不到", "按不到", "按钮", "雨伞", "伞", "工具", "高度"],
        "no_keywords": ["鬼", "坏了", "故障", "停电", "减肥", "锻炼", "恐高", "朋友", "暗号", "钱"],
        "solve_keywords": ["矮", "够不到", "按不到", "雨伞", "伞"],
    },
    {
        "id": "space_beacon",
        "genre": "sci-fi",
        "title": "迟到十年的早安",
        "surface": "宇航员每天早上都会收到空间站自动发来的‘早安’，可今天系统只弹出了一句‘第3650次早安’，他瞬间明白了什么，平静地关掉了返航提醒。发生了什么？",
        "answer": "宇航员处于深空休眠或信号单向延迟中，他以为只过去几天，实际上自动播报已经连续发送了十年，地球家园的时间早已过去了很久。",
        "key_clues": [
            "空间站自动播报每天只发一次",
            "宇航员自己的时间感知与外界不同",
            "3650次代表整整十年的时间已经过去",
        ],
        "hints": [
            "算一算 3650 天大概是多少年？",
            "宇航员并不是每天都在清醒地听播报。",
            "休眠舱或者时间膨胀让时间不知不觉过去了十年。",
        ],
        "yes_keywords": ["十年", "3650", "休眠", "冬眠", "时间", "延迟", "相对论", "醒来", "睡眠", "一天一次"],
        "no_keywords": ["外星人", "病毒", "黑客", "炸弹", "氧气", "幻觉"],
        "solve_keywords": ["十年", "休眠", "冬眠", "时间", "3650"],
    },
    {
        "id": "silent_piano",
        "genre": "mystery",
        "title": "凌晨的钢琴声",
        "surface": "小林搬进老公寓后，每晚两点都能听到楼上准时响起同一首钢琴曲。他忍无可忍敲开楼上的门，却发现屋里根本没有钢琴，也没有人在弹琴。怎么回事？",
        "answer": "楼上住户是一名老调音师，每晚两点定时用老式收音机或黑胶唱片播放自己年轻时的录音来助眠，声音是从音箱传下来的。",
        "key_clues": [
            "钢琴声确实来自楼上房间",
            "声音不是现场弹奏的，而是录音播放",
            "楼上住户依靠这段旋律助眠或怀念过去",
        ],
        "hints": [
            "能响起钢琴声，不一定非得现场有一架钢琴。",
            "时间很规律，每晚两点都会准时响起同一首曲子。",
            "试试猜猜播放设备和定时装置？",
        ],
        "yes_keywords": ["录音", "音箱", "音响", "唱片", "收音机", "手机", "闹钟", "定时", "播放", "音乐"],
        "no_keywords": ["鬼", "灵异", "幻听", "地下室", "隔壁", "凶案", "小偷"],
        "solve_keywords": ["录音", "唱片", "音箱", "音响", "播放"],
    },
    {
        "id": "cold_coffee",
        "genre": "daily",
        "title": "不肯喝的热咖啡",
        "surface": "咖啡店里，侦探刚把一杯滚烫的黑咖啡端给对面的男人，男人却死死盯着杯口，一口也不肯喝，随后转身走出了店门。几分钟后，侦探笑着说：‘破案了。’为什么？",
        "answer": "男人一直自称从小味觉和嗅觉全失，但刚端上来的热咖啡杯里浮着一只苍蝇（或刚加了过量盐），或者男人在等咖啡凉时下意识吹气避开热气却对侦探故意说错的咖啡温度条件露馅——实际上侦探在咖啡里放了一颗只有看得见倒影或能闻到气味才会察觉的异物，男人因为对异常气味/热饮反应露馅。",
        "key_clues": [
            "咖啡本身有被侦探动过手脚的地方",
            "男人之前声称自己有某种感官缺陷",
            "他不敢喝是因为注意到了不该察觉的异常",
        ],
        "hints": [
            "不是有毒，是侦探故意在咖啡里放了东西或者试探他的感官。",
            "如果一个人真的闻不到气味，他会怎么面对一杯普通的黑咖啡？",
            "侦探用热咖啡的气味试探出了他在装病/撒谎。",
        ],
        "yes_keywords": ["撒谎", "试探", "味道", "气味", "嗅觉", "味觉", "装", "侦探", "放了", "加了", "露馅"],
        "no_keywords": ["下毒", "毒药", "太苦", "没钱", "过敏", "烫死"],
        "solve_keywords": ["试探", "嗅觉", "气味", "装", "撒谎", "露馅"],
    },
    {
        "id": "lighthouse_code",
        "genre": "mystery",
        "title": "熄灭的灯塔",
        "surface": "暴风雨夜，偏远海岛上的灯塔突然熄灭了十分钟，但没有一艘船触礁，反而原本偏离航线的一艘货轮安全绕过了暗礁。灯塔管理员为什么故意关灯？",
        "answer": "货轮上的导航仪受到故障信号干扰，误把远处另一座岛的灯光当成了安全航道；管理员发现后故意关灯并用备用信号灯重新校准，让船长意识到灯光位置不对从而及时改道。",
        "key_clues": [
            "灯塔并不是故障，而是管理员主动关灯",
            "货轮原本依据灯光做出错误判断",
            "关灯反而让船长发现航线异常",
        ],
        "hints": [
            "如果是故障，管理员会拼命修；但他是有意熄灭的。",
            "货轮当时正在看着错误的方向或把灯塔误认了。",
            "灯突然消失，反而提醒了船长重新核对位置。",
        ],
        "yes_keywords": ["故意", "提醒", "误认", "导航", "航线", "信号", "暗礁", "船长", "关掉", "校准"],
        "no_keywords": ["停电", "省电", "坏了", "海盗", "鬼船", "睡着"],
        "solve_keywords": ["故意", "提醒", "导航", "误认", "改道", "信号"],
    },
]

_GENRE_ALIASES = {
    "科幻": "sci-fi",
    "sci-fi": "sci-fi",
    "scifi": "sci-fi",
    "日常": "daily",
    "daily": "daily",
    "悬疑": "mystery",
    "推理": "mystery",
    "mystery": "mystery",
}

_GUESS_PREFIX_RE = re.compile(r"^(?:我猜(?:是)?|汤底(?:是)?|真相(?:是)?|答案(?:是)?|破案(?:了)?[：:，,]?)\s*")
_QUESTION_CUE_RE = re.compile(r"[?？]|(?:吗|呢|么)$|(?:是不是|有没有|能不能|可不可以|会不?会|对不对|算不算|是否|难道)")

_QA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["unrelated", "question", "guess"]},
        "verdict": {"type": "string", "enum": ["yes", "no", "irrelevant", "close", "solved"]},
        "unlocked_clue": {"type": ["string", "null"]},
        "spoken_text": {"type": "string"},
    },
    "required": ["kind", "verdict", "spoken_text"],
    "additionalProperties": False,
}

_PUZZLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "genre": {"type": "string"},
        "surface": {"type": "string"},
        "answer": {"type": "string"},
        "key_clues": {"type": "array", "items": {"type": "string"}},
        "hints": {"type": "array", "items": {"type": "string"}},
        "yes_keywords": {"type": "array", "items": {"type": "string"}},
        "no_keywords": {"type": "array", "items": {"type": "string"}},
        "solve_keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["surface", "answer"],
    "additionalProperties": True,
}


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def _normalize_genre(genre: str | None) -> str | None:
    if not genre:
        return None
    cleaned = _normalize_text(genre).casefold()
    return _GENRE_ALIASES.get(cleaned, cleaned or None)


class TurtleSoupService:
    """Interactive lateral-thinking (海龟汤) puzzle game with LLM + deterministic fallback."""

    def __init__(self, daily_memory: Any = None):
        self.daily_memory = daily_memory
        self.state: Literal["idle", "playing", "finished"] = "idle"
        self.current_puzzle: dict[str, Any] | None = None
        self.trigger_user: str = "主播"
        self.clues_found: list[str] = []
        self.qa_history: list[dict[str, Any]] = []
        self.hint_index: int = 0
        self.winner: str | None = None
        self.recent_puzzles: list[str] = []
        self.llm_generated: bool = False

    def is_active(self) -> bool:
        return self.state == "playing" and self.current_puzzle is not None

    def _activate_puzzle(self, puzzle: dict[str, Any], *, trigger_user: str, llm_generated: bool) -> dict[str, Any]:
        self.current_puzzle = puzzle
        self.state = "playing"
        self.trigger_user = trigger_user or "主播"
        self.clues_found = []
        self.qa_history = []
        self.hint_index = 0
        self.winner = None
        self.llm_generated = llm_generated
        if puzzle.get("id"):
            self.recent_puzzles.append(str(puzzle["id"]))
            self.recent_puzzles = self.recent_puzzles[-10:]
        return {
            "status": "started",
            "puzzle_id": puzzle.get("id", "custom"),
            "title": puzzle.get("title", "海龟汤"),
            "genre": puzzle["genre"],
            "surface": puzzle["surface"],
            "trigger_user": self.trigger_user,
            "llm_generated": llm_generated,
            "spoken_text": (
                f"行吧，应{self.trigger_user}要求开一锅海龟汤——听好了，只准问“是、不是或无关”的问题，"
                f"汤面：{puzzle['surface']}"
            ),
        }

    def start_round(self, *, trigger_user: str = "主播", genre: str | None = None) -> dict[str, Any]:
        """Start a sync fallback Turtle Soup round from the built-in clean puzzle bank."""
        target_genre = _normalize_genre(genre)
        pool = [p for p in PUZZLE_BANK if p["id"] not in self.recent_puzzles[-3:]] or list(PUZZLE_BANK)
        if target_genre:
            genre_pool = [p for p in pool if p["genre"] == target_genre]
            if not genre_pool:
                genre_pool = [p for p in PUZZLE_BANK if p["genre"] == target_genre]
            if genre_pool:
                pool = genre_pool
        chosen = dict(random.choice(pool))
        return self._activate_puzzle(chosen, trigger_user=trigger_user, llm_generated=False)

    async def start_round_llm(
        self,
        llm: Any,
        *,
        trigger_user: str = "观众",
        genre: str | None = None,
    ) -> dict[str, Any]:
        """Generate a fresh clean lateral-thinking puzzle via LLM, falling back cleanly on error."""
        if llm is None or not hasattr(llm, "generate_json"):
            return self.start_round(trigger_user=trigger_user, genre=genre)

        genre_hint = _normalize_genre(genre) or "mystery"
        avoid_titles = [
            p["title"] for p in PUZZLE_BANK if p["id"] in self.recent_puzzles[-4:]
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是虚拟主播Aqua的海龟汤出题机。请出一道干净、巧妙、无血腥暴力低俗内容的中文海龟汤（情景推理）题。"
                    "返回严格JSON：title, genre, surface(汤面, 1-3句), answer(汤底, 1-2句), "
                    "key_clues(2-4条), hints(3条渐进提示), yes_keywords, no_keywords, solve_keywords。"
                ),
            },
            {
                "role": "user",
                "content": f"题材偏好：{genre_hint}。请避开已有题目：{avoid_titles}。",
            },
        ]
        try:
            raw = await llm.generate_json(messages, _PUZZLE_SCHEMA, max_tokens=600)
            if not isinstance(raw, dict):
                raise ValueError("invalid payload")
            surface = _normalize_text(str(raw.get("surface") or ""))
            answer = _normalize_text(str(raw.get("answer") or ""))
            if len(surface) < 10 or len(answer) < 8:
                raise ValueError("incomplete puzzle")
            puzzle = {
                "id": f"llm_{len(self.recent_puzzles) + 1}",
                "title": _normalize_text(str(raw.get("title") or "灵感海龟汤"))[:40],
                "genre": _normalize_genre(str(raw.get("genre") or genre_hint)) or genre_hint,
                "surface": surface,
                "answer": answer,
                "key_clues": [str(c).strip() for c in (raw.get("key_clues") or []) if str(c).strip()][:5],
                "hints": [str(h).strip() for h in (raw.get("hints") or []) if str(h).strip()][:4],
                "yes_keywords": [str(k).strip() for k in (raw.get("yes_keywords") or []) if str(k).strip()],
                "no_keywords": [str(k).strip() for k in (raw.get("no_keywords") or []) if str(k).strip()],
                "solve_keywords": [str(k).strip() for k in (raw.get("solve_keywords") or []) if str(k).strip()],
            }
            if not puzzle["hints"]:
                puzzle["hints"] = puzzle["key_clues"] or ["再仔细抠一下汤面里的反常细节。"]
            return self._activate_puzzle(puzzle, trigger_user=trigger_user, llm_generated=True)
        except Exception:
            return self.start_round(trigger_user=trigger_user, genre=genre)

    def _unlock_clue(self, clue: str | None) -> None:
        if not clue:
            return
        cleaned = _normalize_text(clue)
        if cleaned and cleaned not in self.clues_found:
            self.clues_found.append(cleaned)

    def _rule_judge(self, display_name: str, text: str) -> dict[str, Any] | None:
        assert self.current_puzzle is not None
        cleaned = _normalize_text(text)
        if not cleaned:
            return None

        lowered = cleaned.casefold()
        puzzle = self.current_puzzle
        solve_kws = [k.casefold() for k in puzzle.get("solve_keywords", []) if k]
        yes_kws = [k.casefold() for k in puzzle.get("yes_keywords", []) if k]
        no_kws = [k.casefold() for k in puzzle.get("no_keywords", []) if k]

        is_explicit_guess = bool(_GUESS_PREFIX_RE.match(cleaned))
        is_question = bool(_QUESTION_CUE_RE.search(cleaned))
        solve_hits = [k for k in solve_kws if k in lowered]
        yes_hits = [k for k in yes_kws if k in lowered]
        no_hits = [k for k in no_kws if k in lowered]

        if not is_explicit_guess and not is_question and not solve_hits and not yes_hits and not no_hits:
            return None

        # Direct guess or high keyword coverage -> solved / close
        if is_explicit_guess or (len(solve_hits) >= 2 and not is_question):
            if len(solve_hits) >= 2 or (is_explicit_guess and len(solve_hits) >= 1):
                for clue in puzzle.get("key_clues", []):
                    self._unlock_clue(clue)
                return {
                    "kind": "turtle_soup_solved",
                    "verdict": "solved",
                    "spoken_text": (
                        f"啧，居然真被{display_name}一口咬穿汤底了——没错，{puzzle['answer']}"
                    ),
                    "clues_found": list(self.clues_found),
                    "winner": display_name,
                }
            if yes_hits:
                return {
                    "kind": "turtle_soup_qa",
                    "verdict": "close",
                    "spoken_text": f"{display_name}思路摸到边了，再顺着这个方向把真相拼完整！",
                    "clues_found": list(self.clues_found),
                    "winner": None,
                }
            return {
                "kind": "turtle_soup_qa",
                "verdict": "no",
                "spoken_text": f"不对哦{display_name}，这口汤可没这么简单，再想想。",
                "clues_found": list(self.clues_found),
                "winner": None,
            }

        # Question verdict
        if yes_hits:
            clues = puzzle.get("key_clues") or []
            for clue in clues:
                if any(hit in clue.casefold() for hit in yes_hits):
                    self._unlock_clue(clue)
                    break
            else:
                if len(self.clues_found) < len(clues):
                    self._unlock_clue(clues[len(self.clues_found)])
            return {
                "kind": "turtle_soup_qa",
                "verdict": "yes",
                "spoken_text": f"是！{display_name}这问挺敏锐的嘛，勉强夸你一句。",
                "clues_found": list(self.clues_found),
                "winner": None,
            }

        if no_hits:
            return {
                "kind": "turtle_soup_qa",
                "verdict": "no",
                "spoken_text": f"不是哦{display_name}，别往那个方向钻牛角尖啦。",
                "clues_found": list(self.clues_found),
                "winner": None,
            }

        if is_question:
            return {
                "kind": "turtle_soup_qa",
                "verdict": "irrelevant",
                "spoken_text": f"这题跟核心无关啦{display_name}，换个角度再问问？",
                "clues_found": list(self.clues_found),
                "winner": None,
            }

        return None

    async def handle_danmaku(
        self,
        llm: Any,
        *,
        user_id: str | None,
        display_name: str,
        text: str,
    ) -> dict[str, Any] | None:
        """Judge viewer danmaku as question/guess/unrelated; return None for unrelated chatter."""
        if not self.is_active() or not self.current_puzzle:
            return None
        cleaned = _normalize_text(text)
        if not cleaned:
            return None

        result: dict[str, Any] | None = None
        if llm is not None and hasattr(llm, "generate_json"):
            puzzle = self.current_puzzle
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是正在主持海龟汤的虚拟主播Aqua（毒舌但礼貌的傲娇小鲸鱼）。"
                        "判断观众弹幕是：unrelated（普通闲聊，与海龟汤无关）、question（是/否提问）、还是 guess（直接猜汤底）。"
                        "如果是 question/guess，给出 verdict: yes / no / irrelevant / close / solved，"
                        "并生成简短口语化中文回复 spoken_text（不要泄露未猜出的汤底）。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"汤面：{puzzle['surface']}\n"
                        f"汤底：{puzzle['answer']}\n"
                        f"已解锁线索：{self.clues_found}\n"
                        f"观众[{display_name}]说：{cleaned}"
                    ),
                },
            ]
            try:
                raw = await llm.generate_json(messages, _QA_SCHEMA, max_tokens=240)
                if isinstance(raw, dict):
                    kind_raw = str(raw.get("kind") or "").strip().lower()
                    if kind_raw == "unrelated":
                        return None
                    verdict = str(raw.get("verdict") or "").strip().lower()
                    if verdict in {"yes", "no", "irrelevant", "close", "solved"}:
                        if raw.get("unlocked_clue"):
                            self._unlock_clue(str(raw["unlocked_clue"]))
                        spoken = _normalize_text(str(raw.get("spoken_text") or ""))
                        if verdict == "solved":
                            for clue in puzzle.get("key_clues", []):
                                self._unlock_clue(clue)
                            result = {
                                "kind": "turtle_soup_solved",
                                "verdict": "solved",
                                "spoken_text": spoken or f"恭喜{display_name}猜中汤底！{puzzle['answer']}",
                                "clues_found": list(self.clues_found),
                                "winner": display_name,
                            }
                        else:
                            result = {
                                "kind": "turtle_soup_qa",
                                "verdict": verdict,
                                "spoken_text": spoken or f"{display_name}：{verdict}",
                                "clues_found": list(self.clues_found),
                                "winner": None,
                            }
            except Exception:
                result = None

        if result is None:
            result = self._rule_judge(display_name, cleaned)
        if result is None:
            return None

        self.qa_history.append(
            {
                "user": display_name,
                "user_id": user_id,
                "text": cleaned[:160],
                "verdict": result["verdict"],
            }
        )
        if result["verdict"] == "solved":
            self.state = "finished"
            self.winner = display_name
        return result

    def give_hint(self) -> dict[str, Any]:
        """Reveal the next progressive hint for the active puzzle."""
        if not self.current_puzzle or self.state == "idle":
            return {
                "status": "idle",
                "hint": "",
                "hint_index": 0,
                "spoken_text": "现在还没开海龟汤呢，想喝汤的话先喊我开一局吧。",
            }
        hints = self.current_puzzle.get("hints") or self.current_puzzle.get("key_clues") or []
        if not hints:
            hint_text = "多注意汤面里看似顺带一提的小条件，线索就藏在那。"
        elif self.hint_index < len(hints):
            hint_text = hints[self.hint_index]
            self.hint_index += 1
        else:
            hint_text = hints[-1]
        self._unlock_clue(hint_text)
        return {
            "status": "hint",
            "hint": hint_text,
            "hint_index": self.hint_index,
            "total_hints": len(hints),
            "clues_found": list(self.clues_found),
            "spoken_text": f"看你们卡住了，本小姐大发慈悲给个提示：{hint_text}",
        }

    def reveal_and_stop(self, winner: str | None = None) -> dict[str, Any]:
        """Reveal the answer and finish the current Turtle Soup round."""
        if not self.current_puzzle:
            self.state = "idle"
            return {
                "status": "idle",
                "surface": "",
                "answer": "",
                "winner": None,
                "clues_found": [],
                "spoken_text": "当前没有进行中的海龟汤局。",
            }
        self.state = "finished"
        if winner is not None:
            self.winner = winner
        answer = self.current_puzzle["answer"]
        surface = self.current_puzzle["surface"]
        spoken = (
            f"收汤啦！恭喜{self.winner}破案——汤底是：{answer}"
            if self.winner
            else f"好吧好吧揭晓汤底，别说本小姐出题太难哦：{answer}"
        )
        return {
            "status": "finished",
            "surface": surface,
            "answer": answer,
            "winner": self.winner,
            "clues_found": list(self.clues_found),
            "spoken_text": spoken,
        }

    def get_overlay_state(self) -> dict[str, Any]:
        """Return OBS-safe overlay state; answer is exposed only after finishing."""
        puzzle = self.current_puzzle or {}
        finished = self.state == "finished" and bool(self.current_puzzle)
        return {
            "active": self.is_active(),
            "state": self.state,
            "title": puzzle.get("title", ""),
            "surface": puzzle.get("surface", ""),
            "genre": puzzle.get("genre", ""),
            "clues_found": list(self.clues_found),
            "qa_history": list(self.qa_history[-6:]),
            "hint_index": self.hint_index,
            "winner": self.winner,
            "answer": puzzle.get("answer") if finished else None,
        }
