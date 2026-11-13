"""Game livestream adapters and autonomous AI gameplay orchestration (SRS v0.1)."""

import logging
import random
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class GameActionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    params: dict = Field(default_factory=dict)
    executed_by: str = "ai_agent"
    gift_name: str | None = None
    requested_by: str | None = None
    result: str = ""
    timestamp: float = Field(default_factory=time.time)


class BaseGameAdapter:
    name: str = "unknown"
    display_name: str = "未知游戏"

    def __init__(self):
        self.priority_tasks: list[dict[str, Any]] = []
        self.last_action_time: float = time.time()
        self.action_history: list[GameActionRecord] = []
        self.current_activity: str = "正在加载游戏"

    def get_status(self) -> dict[str, Any]:
        raise NotImplementedError

    def decide_and_step(self) -> dict[str, Any]:
        """AI autonomous decision & gameplay step."""
        raise NotImplementedError

    def execute_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def enqueue_priority_action(
        self, action: str, requested_by: str, gift_name: str, params: dict[str, Any] | None = None
    ) -> None:
        self.priority_tasks.append(
            {
                "action": action,
                "requested_by": requested_by,
                "gift_name": gift_name,
                "params": params or {},
                "created_at": time.time(),
            }
        )


class StardewValleyAdapter(BaseGameAdapter):
    """
    Autonomous AI Stardew Valley game adapter.
    Plays farm life, crops, stamina, day/night cycles, and handles viewer gift tasks.
    """

    name = "stardew_valley"
    display_name = "星露谷物语"
    actions = frozenset({"water_crops", "chop_wood", "go_fishing", "eat_snack", "harvest", "sleep_day"})
    save_fields = ("day", "season", "time_str", "minutes", "location", "stamina", "max_stamina", "money",
                   "inventory", "crops", "current_activity")

    def __init__(self):
        super().__init__()
        self.day = 5
        self.season = "春季"
        self.time_str = "08:30"
        self.minutes = 510  # 8:30 AM
        self.location = "农场"
        self.stamina = 85
        self.max_stamina = 100
        self.money = 540
        self.inventory = {
            "防风草种子": 4,
            "防风草": 2,
            "能量小点心": 3,
            "木材": 15,
            "石头": 8,
            "小鱼": 1,
        }
        # 8 crop plots
        self.crops = [
            {"id": i, "name": "防风草", "status": "dry" if i % 2 == 0 else "watered", "days_left": (i % 3)}
            for i in range(8)
        ]
        self.current_activity = "巡视农田并准备浇水"

    def get_status(self) -> dict[str, Any]:
        unwatered = sum(1 for c in self.crops if c["status"] == "dry")
        ready_harvest = sum(1 for c in self.crops if c["days_left"] == 0 and c["status"] == "watered")
        summary = (
            f"《星露谷物语》{self.season}第{self.day}天 {self.time_str}，在{self.location}。"
            f"正在{self.current_activity}。体力: {self.stamina}/{self.max_stamina}，金钱: {self.money}g。"
            f"农田待浇水: {unwatered}块，可收获: {ready_harvest}块。"
        )
        return {
            "game": self.name,
            "display_name": self.display_name,
            "summary": summary,
            "day": self.day,
            "season": self.season,
            "time": self.time_str,
            "location": self.location,
            "stamina": self.stamina,
            "money": self.money,
            "current_activity": self.current_activity,
            "inventory": self.inventory,
            "crops_count": len(self.crops),
            "unwatered_crops": unwatered,
            "ready_harvest": ready_harvest,
            "priority_tasks": len(self.priority_tasks),
        }

    def _advance_time(self, mins: int = 20):
        self.minutes += mins
        if self.minutes >= 1440:  # 24:00
            self._sleep_to_next_day()
            return
        hours = self.minutes // 60
        rem = self.minutes % 60
        self.time_str = f"{hours:02d}:{rem:02d}"

    def _sleep_to_next_day(self):
        self.day += 1
        self.minutes = 360  # 6:00 AM
        self.time_str = "06:00"
        self.stamina = self.max_stamina
        self.location = "农场小屋"
        # Reset crops status for new day
        for c in self.crops:
            if c["status"] == "watered" and c["days_left"] > 0:
                c["days_left"] -= 1
            c["status"] = "dry"
        self.current_activity = "新的一天开始了，在大床上伸懒腰起床"

    def execute_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self.last_action_time = time.time()

        if action == "water_crops":
            self.location = "农田"
            watered_any = False
            for c in self.crops:
                if c["status"] == "dry" and self.stamina >= 5:
                    c["status"] = "watered"
                    self.stamina = max(0, self.stamina - 5)
                    watered_any = True
            self._advance_time(30)
            self.current_activity = "给农田里的防风草浇好了水"
            result = "成功给农作物浇水，土地变得湿润了" if watered_any else "农作物已经都浇过水啦"
            return {"success": True, "action": action, "message": result}

        elif action == "chop_wood":
            self.location = "农场林地"
            if self.stamina >= 10:
                self.stamina = max(0, self.stamina - 10)
                gained = random.randint(4, 8)
                self.inventory["木材"] = self.inventory.get("木材", 0) + gained
                self._advance_time(40)
                self.current_activity = f"抡起斧头砍倒了一棵大树，获得{gained}个木材"
                return {"success": True, "action": action, "message": f"获得木材 x{gained}"}
            else:
                return {"success": False, "action": action, "message": "体力不足，挥不动斧头啦"}

        elif action == "go_fishing":
            self.location = "小河边"
            if self.stamina >= 8:
                self.stamina = max(0, self.stamina - 8)
                fishes = ["太阳鱼", "大嘴鲈鱼", "鲱鱼", "小金鱼"]
                catch = random.choice(fishes)
                self.inventory[catch] = self.inventory.get(catch, 0) + 1
                self._advance_time(45)
                self.current_activity = f"甩出鱼竿耐心地钓上了一条【{catch}】"
                return {"success": True, "action": action, "message": f"钓到了一条【{catch}】！"}
            else:
                return {"success": False, "action": action, "message": "体力见底，钓鱼收不动线了"}

        elif action == "eat_snack":
            if self.inventory.get("能量小点心", 0) > 0:
                self.inventory["能量小点心"] -= 1
                self.stamina = min(self.max_stamina, self.stamina + 35)
                self._advance_time(10)
                self.current_activity = "开心地吃下了一块能量小点心，恢复了体力"
                return {"success": True, "action": action, "message": "吃下点心，体力 +35！"}
            else:
                return {"success": False, "action": action, "message": "背包里没有能量小点心了"}

        elif action == "harvest":
            self.location = "农田"
            harvested = 0
            for c in self.crops:
                if c["days_left"] == 0 and c["status"] == "watered":
                    harvested += 1
                    c["days_left"] = 4
                    c["status"] = "dry"
            if harvested > 0:
                self.inventory["防风草"] = self.inventory.get("防风草", 0) + harvested
                self._advance_time(20)
                self.current_activity = f"喜获丰收！收割了{harvested}株水灵灵的防风草"
                return {"success": True, "action": action, "message": f"收获防风草 x{harvested}"}
            else:
                return {"success": False, "action": action, "message": "地里的防风草还在茁壮成长中"}

        elif action == "sleep_day":
            self._sleep_to_next_day()
            return {"success": True, "action": action, "message": f"上床睡觉，迎来第{self.day}天"}

        return {"success": False, "action": action, "message": f"未知动作: {action}"}

    def decide_and_step(self) -> dict[str, Any]:
        """Autonomous AI game loop."""
        # 1. Check priority gift tasks from audience
        if self.priority_tasks:
            task = self.priority_tasks.pop(0)
            res = self.execute_action(task["action"], task.get("params"))
            record = GameActionRecord(
                action=task["action"],
                params=task.get("params", {}),
                executed_by="audience_gift",
                gift_name=task.get("gift_name"),
                requested_by=task.get("requested_by"),
                result=res.get("message", ""),
            )
            self.action_history.append(record)
            return {"type": "priority_task", "task": task, "result": res}

        # 2. Daytime logic
        # Late night or exhausted -> Sleep
        if self.minutes >= 1320 or self.stamina <= 5:  # 22:00
            res = self.execute_action("sleep_day")
            return {"type": "routine", "action": "sleep_day", "result": res}

        # Low stamina -> Eat snack if available
        if self.stamina < 20 and self.inventory.get("能量小点心", 0) > 0:
            res = self.execute_action("eat_snack")
            return {"type": "routine", "action": "eat_snack", "result": res}

        # Harvest ready crops
        ready_crops = [c for c in self.crops if c["days_left"] == 0 and c["status"] == "watered"]
        if ready_crops:
            res = self.execute_action("harvest")
            return {"type": "routine", "action": "harvest", "result": res}

        # Morning water crops
        unwatered = [c for c in self.crops if c["status"] == "dry"]
        if unwatered and self.stamina >= 10:
            res = self.execute_action("water_crops")
            return {"type": "routine", "action": "water_crops", "result": res}

        # Afternoon / Free time: chop wood or fish
        if self.minutes < 1080 and self.stamina >= 15:  # Before 18:00
            choice = "go_fishing" if self.day % 2 == 0 else "chop_wood"
            res = self.execute_action(choice)
            return {"type": "routine", "action": choice, "result": res}

        # Idle evening walk
        self._advance_time(30)
        self.location = "小镇广场"
        self.current_activity = "在鹈鹕镇散步吹吹晚风，和村民们打招呼"
        return {"type": "routine", "action": "walk_town", "result": {"success": True, "message": "散步巡视"}}


class PvZAdapter(BaseGameAdapter):
    """
    Autonomous AI Plants vs Zombies game adapter.
    Plays lawn defense, sun economy, plants sunflowers, peashooters, wallnuts, cherry bombs.
    """

    name = "pvz"
    display_name = "植物大战僵尸"
    actions = frozenset({"plant_sunflower", "plant_peashooter", "plant_wallnut", "use_cherry_bomb"})
    save_fields = ("level", "sun", "wave", "total_waves", "grid", "zombies", "current_activity")

    def __init__(self):
        super().__init__()
        self.level = "1-3 (白天草坪)"
        self.sun = 175
        self.wave = 2
        self.total_waves = 8
        # 5 rows x 9 columns grid
        self.grid = [[None for _ in range(9)] for _ in range(5)]
        # Initial starter plants
        self.grid[1][0] = "向日葵"
        self.grid[2][0] = "向日葵"
        self.grid[2][1] = "豌豆射手"
        # Incoming zombies: list of {"row": int, "col": float, "hp": int, "type": str}
        self.zombies = [
            {"row": 0, "col": 8.0, "hp": 10, "type": "普通僵尸"},
            {"row": 3, "col": 7.5, "hp": 15, "type": "路障僵尸"},
        ]
        self.current_activity = "正在观察僵尸走向并种植向日葵"

    def get_status(self) -> dict[str, Any]:
        sunflower_count = sum(row.count("向日葵") for row in self.grid)
        peashooter_count = sum(row.count("豌豆射手") for row in self.grid)
        wallnut_count = sum(row.count("坚果墙") for row in self.grid)
        summary = (
            f"《植物大战僵尸》关卡: {self.level}，波次: {self.wave}/{self.total_waves}。"
            f"当前阳光: {self.sun}。前线僵尸: {len(self.zombies)}只。"
            f"防线配置: 向日葵x{sunflower_count}, 豌豆射手x{peashooter_count}, 坚果墙x{wallnut_count}。"
            f"正在{self.current_activity}。"
        )
        return {
            "game": self.name,
            "display_name": self.display_name,
            "summary": summary,
            "level": self.level,
            "wave": self.wave,
            "total_waves": self.total_waves,
            "sun": self.sun,
            "zombies_count": len(self.zombies),
            "zombies": self.zombies,
            "current_activity": self.current_activity,
            "sunflowers": sunflower_count,
            "peashooters": peashooter_count,
            "wallnuts": wallnut_count,
            "priority_tasks": len(self.priority_tasks),
        }

    def execute_action(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        self.last_action_time = time.time()

        if action == "plant_sunflower":
            if self.sun >= 50:
                # Find empty slot in col 0-1
                for r in range(5):
                    for c in (0, 1):
                        if self.grid[r][c] is None:
                            self.grid[r][c] = "向日葵"
                            self.sun -= 50
                            self.current_activity = f"在第{r + 1}行种下了一棵萌萌的向日葵"
                            return {"success": True, "action": action, "message": f"种下向日葵 (第{r + 1}行)"}
                return {"success": False, "action": action, "message": "后排已经种满啦"}
            else:
                return {"success": False, "action": action, "message": "阳光不足 (需50阳光)"}

        elif action == "plant_peashooter":
            target_row = params.get("row")
            if target_row is None:
                # Prioritize row with zombies
                zombie_rows = [z["row"] for z in self.zombies]
                target_row = zombie_rows[0] if zombie_rows else 2

            if self.sun >= 100:
                for c in range(2, 6):
                    if self.grid[target_row][c] is None:
                        self.grid[target_row][c] = "豌豆射手"
                        self.sun -= 100
                        self.current_activity = f"在第{target_row + 1}行种植了豌豆射手，开始射击！"
                        return {
                            "success": True,
                            "action": action,
                            "message": f"种下豌豆射手 (第{target_row + 1}行)",
                        }
                return {"success": False, "action": action, "message": "该行没有空余防御位置"}
            else:
                return {"success": False, "action": action, "message": "阳光不足 (需100阳光)"}

        elif action == "plant_wallnut":
            target_row = params.get("row")
            if target_row is None:
                zombie_rows = [z["row"] for z in self.zombies]
                target_row = zombie_rows[0] if zombie_rows else random.randint(0, 4)

            if self.sun >= 50:
                for c in (5, 6, 4):
                    if self.grid[target_row][c] is None:
                        self.grid[target_row][c] = "坚果墙"
                        self.sun -= 50
                        self.current_activity = f"在第{target_row + 1}行坚毅地立起了一堵坚果墙！"
                        return {
                            "success": True,
                            "action": action,
                            "message": f"种下坚果墙 (第{target_row + 1}行)",
                        }
                return {"success": False, "action": action, "message": "该位置已存在植物"}
            else:
                return {"success": False, "action": action, "message": "阳光不足 (需50阳光)"}

        elif action == "use_cherry_bomb":
            if self.sun >= 150:
                self.sun -= 150
                cleared = len(self.zombies)
                self.zombies.clear()
                self.current_activity = f"砰！引爆了樱桃炸弹，清屏消灭了{cleared}只僵尸！"
                return {"success": True, "action": action, "message": f"樱桃炸弹爆炸！消灭僵尸 x{cleared}"}
            else:
                return {"success": False, "action": action, "message": "阳光不足 (需150阳光)"}

        return {"success": False, "action": action, "message": f"未知动作: {action}"}

    def decide_and_step(self) -> dict[str, Any]:
        """Autonomous AI game loop."""
        # 1. Process fallen sun and sunflower production
        sunflowers = sum(row.count("向日葵") for row in self.grid)
        self.sun += 25 + (sunflowers * 10)

        # 2. Check audience gift tasks
        if self.priority_tasks:
            task = self.priority_tasks.pop(0)
            res = self.execute_action(task["action"], task.get("params"))
            record = GameActionRecord(
                action=task["action"],
                params=task.get("params", {}),
                executed_by="audience_gift",
                gift_name=task.get("gift_name"),
                requested_by=task.get("requested_by"),
                result=res.get("message", ""),
            )
            self.action_history.append(record)
            return {"type": "priority_task", "task": task, "result": res}

        # 3. Simulate zombie march and defense
        for z in list(self.zombies):
            z["col"] -= 0.5
            # Peas damage zombies
            row = z["row"]
            peashooters = self.grid[row].count("豌豆射手")
            z["hp"] -= peashooters * 3
            if z["hp"] <= 0:
                self.zombies.remove(z)
                self.current_activity = f"豌豆射手击溃了第{row + 1}行的僵尸"

        # Spawn new zombie occasionally
        if len(self.zombies) < 3 and random.random() < 0.4:
            new_row = random.randint(0, 4)
            self.zombies.append({"row": new_row, "col": 8.5, "hp": 10, "type": "普通僵尸"})

        # Emergency cherry bomb if zombies getting close (col < 2)
        breaching = [z for z in self.zombies if z["col"] < 2.5]
        if breaching and self.sun >= 150:
            res = self.execute_action("use_cherry_bomb")
            return {"type": "routine", "action": "use_cherry_bomb", "result": res}

        # Defense: If a row has zombies but no peashooter, plant peashooter
        for z in self.zombies:
            row = z["row"]
            if "豌豆射手" not in self.grid[row] and self.sun >= 100:
                res = self.execute_action("plant_peashooter", {"row": row})
                return {"type": "routine", "action": "plant_peashooter", "result": res}

        # Wallnut defense if zombies are at col <= 4
        approaching = [z for z in self.zombies if z["col"] <= 4.5]
        if approaching and self.sun >= 50:
            row = approaching[0]["row"]
            if "坚果墙" not in self.grid[row]:
                res = self.execute_action("plant_wallnut", {"row": row})
                return {"type": "routine", "action": "plant_wallnut", "result": res}

        # Economy: Keep planting sunflowers up to 4
        if sunflowers < 4 and self.sun >= 50:
            res = self.execute_action("plant_sunflower")
            return {"type": "routine", "action": "plant_sunflower", "result": res}

        # Extra firepower: add more peashooters
        if self.sun >= 100:
            for r in range(5):
                if self.grid[r].count("豌豆射手") < 2:
                    res = self.execute_action("plant_peashooter", {"row": r})
                    return {"type": "routine", "action": "plant_peashooter", "result": res}

        self.current_activity = f"坚守草坪，后排阳光充沛 ({self.sun})，等待下一波僵尸"
        return {"type": "routine", "action": "watch_lawn", "result": {"success": True, "message": "巡视草坪"}}


class GameLivestreamService:
    def __init__(self, interactive_rules=None, store=None):
        self.rules_service = interactive_rules
        self.store = store
        self.adapters: dict[str, BaseGameAdapter] = {
            "stardew_valley": StardewValleyAdapter(),
            "pvz": PvZAdapter(),
        }
        self.active_game_id: str = "stardew_valley"
        self.is_playing: bool = True
        self.owner = "studio"
        self.last_handoff = None
        if store and (saved := store.get("simulation_save")):
            self.active_game_id = saved["game"]
            self.owner = saved["owner"]
            self.last_handoff = saved.get("last_handoff")
            for game, adapter in self.adapters.items():
                for field, value in saved["adapters"].get(game, {}).items():
                    if field in adapter.save_fields:
                        setattr(adapter, field, value)

    def _save(self):
        if self.store:
            self.store.set("simulation_save", {
                "game": self.active_game_id, "owner": self.owner, "last_handoff": self.last_handoff,
                "adapters": {name: {field: getattr(adapter, field) for field in adapter.save_fields}
                             for name, adapter in self.adapters.items()},
            })

    def status(self):
        return {"active_game_id": self.active_game_id, "is_active": self.is_active(),
                "active_status": self.get_active_adapter().get_status(), "owner": self.owner,
                "available_games": [{"id": key, "display_name": adapter.display_name}
                                    for key, adapter in self.adapters.items()]}

    def transfer_control(self, owner):
        if owner not in {"studio", "operator"}:
            raise ValueError("游戏控制权应为工作台或号主")
        self.owner = owner
        self._save()
        return self.status()

    def handoff(self, request_id, game, action):
        if self.last_handoff and self.last_handoff["request"] == request_id:
            return self.last_handoff["result"]
        if self.owner != "studio" or game != self.active_game_id:
            raise ValueError("当前游戏已切换或由号主接管")
        adapter = self.get_active_adapter()
        if action != "step" and action not in adapter.actions:
            raise ValueError("当前游戏不支持这个动作")
        if action == "step":
            step = adapter.decide_and_step()
            result = step["result"] | {"action": step.get("action") or step["result"].get("action")}
        else:
            result = adapter.execute_action(action)
        result = result | {"game": game, "state": adapter.get_status()["summary"]}
        self.last_handoff = {"request": request_id, "result": result}
        self._save()
        return result

    def manual_action(self, action, params=None):
        result = self.get_active_adapter().execute_action(action, params)
        self._save()
        return result

    def get_active_adapter(self) -> BaseGameAdapter:
        return self.adapters.get(self.active_game_id, self.adapters["stardew_valley"])

    def is_active(self) -> bool:
        return self.is_playing and (self.active_game_id in self.adapters)

    def switch_game(self, target_game_id: str, reason: str = "观众点播/切换") -> dict[str, Any]:
        if target_game_id not in self.adapters:
            raise ValueError(f"未找到目标游戏: {target_game_id}，可用游戏: {list(self.adapters.keys())}")

        old_game = self.get_active_adapter().display_name
        self.active_game_id = target_game_id
        self._save()
        new_game = self.get_active_adapter().display_name

        spoken_text = f"收到！大肥鱼这就切换游戏画面！暂别《{old_game}》，马上进入《{new_game}》！"
        return {
            "status": "switched",
            "previous_game": old_game,
            "current_game_id": target_game_id,
            "current_game_name": new_game,
            "spoken_text": spoken_text,
            "reason": reason,
        }

    def handle_gift_action(self, action: str, label: str, user_name: str, gift_name: str) -> dict[str, Any]:
        """Injects audience gift bounty into active game adapter."""
        adapter = self.get_active_adapter()
        adapter.enqueue_priority_action(
            action=action,
            requested_by=user_name,
            gift_name=gift_name,
        )
        spoken_text = f"感谢【{user_name}】送的【{gift_name}】！大肥鱼这就去安排【{label}】！看本鱼秀操作！"
        return {
            "status": "enqueued",
            "action": action,
            "label": label,
            "user_name": user_name,
            "gift_name": gift_name,
            "spoken_text": spoken_text,
        }

    def get_context_prompt(self) -> str:
        """Injects game status into LLM context."""
        if not self.is_active():
            return ""
        adapter = self.get_active_adapter()
        st = adapter.get_status()
        return (
            f"[当前正在直播游戏: {st['display_name']}]\n"
            f"游戏状态: {st['summary']}"
        )

    def step_game(self) -> dict[str, Any]:
        """Steps the active game."""
        if self.owner == "operator":
            raise ValueError("当前由号主接管，请先交回工作台控制")
        adapter = self.get_active_adapter()
        result = adapter.decide_and_step()
        self._save()
        return result
