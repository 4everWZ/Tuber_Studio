
from tuber_studio.live.game_livestream import (
    GameLivestreamService,
    PvZAdapter,
    StardewValleyAdapter,
)


def test_stardew_valley_adapter_actions():
    adapter = StardewValleyAdapter()
    st = adapter.get_status()
    assert st["game"] == "stardew_valley"
    assert st["stamina"] == 85
    assert "星露谷物语" in st["summary"]

    # Water crops
    res = adapter.execute_action("water_crops")
    assert res["success"] is True
    assert adapter.stamina < 85

    # Chop wood
    res = adapter.execute_action("chop_wood")
    assert res["success"] is True
    assert adapter.inventory["木材"] > 15

    # Eat snack
    res = adapter.execute_action("eat_snack")
    assert res["success"] is True

    # Sleep day
    res = adapter.execute_action("sleep_day")
    assert res["success"] is True
    assert adapter.day == 6
    assert adapter.stamina == adapter.max_stamina


def test_stardew_valley_priority_task():
    adapter = StardewValleyAdapter()
    adapter.enqueue_priority_action("go_fishing", requested_by="钓鱼佬", gift_name="鱼饵")
    assert len(adapter.priority_tasks) == 1

    step_res = adapter.decide_and_step()
    assert step_res["type"] == "priority_task"
    assert step_res["task"]["action"] == "go_fishing"
    assert len(adapter.priority_tasks) == 0
    assert len(adapter.action_history) == 1
    assert adapter.action_history[0].requested_by == "钓鱼佬"


def test_pvz_adapter_actions():
    adapter = PvZAdapter()
    st = adapter.get_status()
    assert st["game"] == "pvz"
    assert st["sun"] == 175
    assert "植物大战僵尸" in st["summary"]

    # Plant sunflower
    res = adapter.execute_action("plant_sunflower")
    assert res["success"] is True
    assert adapter.sun == 125

    # Plant peashooter
    res = adapter.execute_action("plant_peashooter", {"row": 0})
    assert res["success"] is True
    assert adapter.sun == 25

    # Cherry bomb requires 150 sun
    fail_res = adapter.execute_action("use_cherry_bomb")
    assert fail_res["success"] is False
    assert "阳光不足" in fail_res["message"]

    # Give sun and use cherry bomb
    adapter.sun = 200
    bomb_res = adapter.execute_action("use_cherry_bomb")
    assert bomb_res["success"] is True
    assert len(adapter.zombies) == 0


def test_pvz_priority_task():
    adapter = PvZAdapter()
    adapter.enqueue_priority_action("plant_wallnut", requested_by="防守专家", gift_name="坚果")
    assert len(adapter.priority_tasks) == 1

    step_res = adapter.decide_and_step()
    assert step_res["type"] == "priority_task"
    assert step_res["task"]["action"] == "plant_wallnut"
    assert len(adapter.priority_tasks) == 0


def test_game_livestream_service():
    service = GameLivestreamService()
    assert service.active_game_id == "stardew_valley"

    # Context prompt
    prompt = service.get_context_prompt()
    assert "当前正在直播游戏: 星露谷物语" in prompt
    assert "若观众问你在干嘛" in prompt

    # Handle gift action
    act_res = service.handle_gift_action("water_crops", "浇水", "观众小明", "小心心")
    assert act_res["status"] == "enqueued"
    assert "观众小明" in act_res["spoken_text"]
    assert "浇水" in act_res["spoken_text"]

    adapter = service.get_active_adapter()
    assert len(adapter.priority_tasks) == 1

    # Switch game
    sw_res = service.switch_game("pvz")
    assert sw_res["status"] == "switched"
    assert service.active_game_id == "pvz"
    assert "植物大战僵尸" in sw_res["spoken_text"]

    # Step active game
    step_res = service.step_game()
    assert "type" in step_res
