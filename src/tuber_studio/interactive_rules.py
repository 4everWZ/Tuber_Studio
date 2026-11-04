"""Interactive and gift trigger rules for song requests, mini-games, and game livestreaming."""

import re
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from .models import Event


class SongRequestRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["free", "gift_only", "gift_or_free"] = "free"
    min_coin: int = Field(default=0, ge=0, le=100000)
    gift_names: list[str] = Field(default_factory=list, max_length=20)
    danmaku_command: str = Field(default="点歌", max_length=20)


class DrawAndGuessRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    trigger_mode: Literal["gift_only", "danmaku_or_gift", "host_only"] = "gift_only"
    min_coin: int = Field(default=10, ge=0, le=100000)
    gift_names: list[str] = Field(default_factory=lambda: ["画笔", "小心心", "辣条"], max_length=20)
    danmaku_command: str = Field(default="#你画我猜", max_length=30)
    guess_mode: Literal["free", "gift_only"] = "free"
    round_duration_sec: int = Field(default=60, ge=15, le=300)
    cooldown_sec: int = Field(default=30, ge=0, le=600)


class GiftActionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=50)
    label: str = Field(min_length=1, max_length=50)
    min_coin: int = Field(default=10, ge=0, le=100000)
    gift_names: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=20)


class GameConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    name: str = Field(min_length=1, max_length=50)
    mode: Literal["auto_play", "on_demand"] = "auto_play"
    switch_min_coin: int = Field(default=0, ge=0, le=100000)
    switch_gift_names: list[str] = Field(default_factory=list, max_length=20)
    switch_command: str = Field(default="", max_length=30)
    gift_actions: list[GiftActionRule] = Field(default_factory=list, max_length=30)


class GameLivestreamRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    active_game: str = Field(default="stardew_valley", max_length=50)
    games: dict[str, GameConfig] = Field(default_factory=dict)


class InteractiveRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    song_request: SongRequestRule = Field(default_factory=SongRequestRule)
    draw_and_guess: DrawAndGuessRule = Field(default_factory=DrawAndGuessRule)
    game_livestream: GameLivestreamRule = Field(default_factory=GameLivestreamRule)


def default_rules() -> InteractiveRules:
    return InteractiveRules(
        song_request=SongRequestRule(
            mode="free",
            min_coin=0,
            gift_names=[],
            danmaku_command="点歌",
        ),
        draw_and_guess=DrawAndGuessRule(
            enabled=True,
            trigger_mode="gift_only",
            min_coin=10,
            gift_names=["画笔", "小心心", "辣条"],
            danmaku_command="#你画我猜",
            guess_mode="free",
            round_duration_sec=60,
            cooldown_sec=30,
        ),
        game_livestream=GameLivestreamRule(
            enabled=True,
            active_game="stardew_valley",
            games={
                "stardew_valley": GameConfig(
                    enabled=True,
                    name="星露谷物语",
                    mode="auto_play",
                    switch_min_coin=50,
                    switch_gift_names=["冰阔落", "牛哇牛哇"],
                    switch_command="#玩星露谷",
                    gift_actions=[
                        GiftActionRule(
                            action="water_crops",
                            label="浇水",
                            min_coin=10,
                            gift_names=["小心心", "辣条", "小雨滴"],
                            keywords=["浇水", "浇地", "去浇水"],
                        ),
                        GiftActionRule(
                            action="chop_wood",
                            label="砍树",
                            min_coin=20,
                            gift_names=["木头", "斧头"],
                            keywords=["砍树", "伐木", "去砍树"],
                        ),
                        GiftActionRule(
                            action="go_fishing",
                            label="钓鱼",
                            min_coin=50,
                            gift_names=["鱼饵", "大肥鱼"],
                            keywords=["钓鱼", "抓鱼", "去钓鱼"],
                        ),
                        GiftActionRule(
                            action="eat_snack",
                            label="吃点心补体力",
                            min_coin=10,
                            gift_names=["吃瓜", "辣条"],
                            keywords=["吃点心", "补体力", "吃点东西"],
                        ),
                    ],
                ),
                "pvz": GameConfig(
                    enabled=True,
                    name="植物大战僵尸",
                    mode="on_demand",
                    switch_min_coin=50,
                    switch_gift_names=["向日葵", "冰阔落"],
                    switch_command="#玩植物大战僵尸",
                    gift_actions=[
                        GiftActionRule(
                            action="plant_sunflower",
                            label="种向日葵",
                            min_coin=10,
                            gift_names=["小心心", "太阳", "向日葵"],
                            keywords=["种向日葵", "多种向日葵", "产阳光"],
                        ),
                        GiftActionRule(
                            action="plant_wallnut",
                            label="放坚果墙",
                            min_coin=20,
                            gift_names=["坚果", "盾牌"],
                            keywords=["放坚果", "种坚果", "坚果墙"],
                        ),
                        GiftActionRule(
                            action="use_cherry_bomb",
                            label="放樱桃炸弹",
                            min_coin=50,
                            gift_names=["炸弹", "火箭"],
                            keywords=["放炸弹", "樱桃炸弹", "炸死他们"],
                        ),
                    ],
                ),
            },
        ),
    )


class InteractiveRulesService:
    def __init__(self, store):
        self.store = store

    def get_rules(self) -> InteractiveRules:
        raw = self.store.get("interactive_rules")
        if not raw:
            return default_rules()
        try:
            return InteractiveRules.model_validate(raw)
        except Exception:
            return default_rules()

    def save_rules(self, rules: InteractiveRules | dict) -> dict:
        if isinstance(rules, dict):
            validated = InteractiveRules.model_validate(rules)
        else:
            validated = rules
        data = validated.model_dump()
        self.store.set("interactive_rules", data)
        return data

    def extract_gift_info(self, event: Event) -> tuple[str, int, int]:
        """Returns (gift_name, coin_amount, count)."""
        metadata = event.metadata or {}
        gift_name = metadata.get("gift_name") or metadata.get("name") or event.text or ""
        coin = int(metadata.get("price") or metadata.get("coin") or metadata.get("total_coin") or 0)
        num = int(metadata.get("num") or metadata.get("count") or 1)
        return gift_name.strip(), coin, num

    def match_song_request(self, event: Event) -> tuple[bool, str | None, str | None]:
        """
        Check if event is a song request and whether it meets configured rules.
        Returns: (is_song_request, song_query_or_none, rejection_reason_or_none)
        """
        rules = self.get_rules().song_request
        cmd_prefix = rules.danmaku_command.strip() or "点歌"

        song_title = None
        if event.type == "chat":
            match = re.fullmatch(rf"\s*{re.escape(cmd_prefix)}\s+(.{{1,100}}?)\s*", event.text)
            if match:
                song_title = match.group(1).strip()
        elif event.type == "gift":
            # Check if gift message or text specifies a song
            match = re.search(rf"(?:{re.escape(cmd_prefix)})\s+(.{{1,100}}?)(?:$|\s)", event.text)
            if match:
                song_title = match.group(1).strip()

        if not song_title:
            return False, None, None

        # Verify threshold
        if rules.mode == "gift_only":
            if event.type != "gift":
                req_names = "、".join(rules.gift_names) if rules.gift_names else "指定礼物"
                reason = f"当前点歌需要送出礼物（如【{req_names}】或价值 ≥ {rules.min_coin} 电池）"
                return True, song_title, reason
            gift_name, coin, _ = self.extract_gift_info(event)
            if rules.gift_names and gift_name not in rules.gift_names and coin < rules.min_coin:
                req_names = "、".join(rules.gift_names)
                reason = f"点歌需要送出【{req_names}】或价值 ≥ {rules.min_coin} 电池"
                return True, song_title, reason
            if not rules.gift_names and coin < rules.min_coin:
                reason = f"点歌需要送出价值 ≥ {rules.min_coin} 电池的礼物"
                return True, song_title, reason

        return True, song_title, None

    def match_draw_and_guess_start(self, event: Event) -> tuple[bool, str | None]:
        """
        Check if event matches rules to trigger starting Draw & Guess.
        Returns: (should_start, reason_or_none)
        """
        rules = self.get_rules().draw_and_guess
        if not rules.enabled:
            return False, None

        if rules.trigger_mode == "host_only":
            return False, None

        if event.type == "chat":
            if rules.trigger_mode == "danmaku_or_gift":
                if event.text.strip().lower() == rules.danmaku_command.lower():
                    return True, None
            return False, None

        if event.type == "gift":
            gift_name, coin, _ = self.extract_gift_info(event)
            is_cmd = event.text.strip().lower() == rules.danmaku_command.lower()
            is_named_gift = bool(rules.gift_names and gift_name in rules.gift_names)
            is_coin_threshold = bool(coin >= rules.min_coin > 0)
            if is_cmd or is_named_gift or is_coin_threshold:
                return True, None

        return False, None

    def match_game_switch(self, event: Event) -> tuple[str | None, str | None]:
        """
        Check if event requests switching game.
        Returns: (target_game_id_or_none, target_game_name_or_none)
        """
        gl_rules = self.get_rules().game_livestream
        if not gl_rules.enabled:
            return None, None

        text = event.text.strip()
        gift_name, coin, _ = self.extract_gift_info(event)

        for gid, gconfig in gl_rules.games.items():
            if not gconfig.enabled:
                continue
            # Danmaku command switch
            if gconfig.switch_command and (text == gconfig.switch_command or f"换玩{gconfig.name}" in text):
                return gid, gconfig.name

            # Gift switch
            if event.type == "gift":
                if gconfig.switch_gift_names and gift_name in gconfig.switch_gift_names:
                    return gid, gconfig.name
                if coin >= gconfig.switch_min_coin > 0 and (
                    gconfig.name in text or "换游戏" in text or "玩" + gconfig.name in text
                ):
                    return gid, gconfig.name

        return None, None

    def match_game_action(self, event: Event, current_game_id: str) -> tuple[str | None, str | None]:
        """
        Check if gift specifies an in-game bounty/action.
        Returns: (action_name_or_none, action_label_or_none)
        """
        gl_rules = self.get_rules().game_livestream
        if not gl_rules.enabled or current_game_id not in gl_rules.games:
            return None, None

        game_cfg = gl_rules.games[current_game_id]
        if not game_cfg.enabled:
            return None, None

        text = event.text.strip()
        gift_name, coin, _ = self.extract_gift_info(event)

        for rule in game_cfg.gift_actions:
            matched_gift = rule.gift_names and gift_name in rule.gift_names
            matched_coin = coin >= rule.min_coin if rule.min_coin > 0 else True
            matched_kw = any(kw in text for kw in rule.keywords) if rule.keywords else False

            # Direct match: Gift name matches and message mentions action, or message strongly specifies keyword with coin threshold
            if (matched_gift and (matched_kw or not rule.keywords)) or (matched_kw and matched_coin):
                return rule.action, rule.label

        return None, None
