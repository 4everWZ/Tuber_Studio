import pytest

from tuber_studio.interactive_rules import (
    InteractiveRulesService,
    default_rules,
)
from tuber_studio.models import Event


@pytest.fixture
def service(store):
    return InteractiveRulesService(store)


def test_default_rules(service):
    rules = service.get_rules()
    assert rules.song_request.mode == "free"
    assert rules.draw_and_guess.enabled is True
    assert "stardew_valley" in rules.game_livestream.games
    assert "pvz" in rules.game_livestream.games


def test_save_and_retrieve_rules(service):
    rules = default_rules()
    rules.song_request.mode = "gift_only"
    rules.song_request.min_coin = 50
    rules.song_request.gift_names = ["小心心", "辣条"]
    service.save_rules(rules)

    retrieved = service.get_rules()
    assert retrieved.song_request.mode == "gift_only"
    assert retrieved.song_request.min_coin == 50
    assert retrieved.song_request.gift_names == ["小心心", "辣条"]


def test_song_request_free_mode(service):
    chat_ev = Event(type="chat", text="点歌 晴天", display_name="小李")
    is_req, query, rej = service.match_song_request(chat_ev)
    assert is_req is True
    assert query == "晴天"
    assert rej is None


def test_song_request_gift_only_mode(service):
    rules = default_rules()
    rules.song_request.mode = "gift_only"
    rules.song_request.min_coin = 20
    rules.song_request.gift_names = ["小心心"]
    service.save_rules(rules)

    # Ordinary chat should be rejected with clear message
    chat_ev = Event(type="chat", text="点歌 七里香", display_name="小张")
    is_req, query, rej = service.match_song_request(chat_ev)
    assert is_req is True
    assert query == "七里香"
    assert "当前点歌需要送出礼物" in rej

    # Gift with matching gift_name should pass
    gift_ev = Event(
        type="gift",
        text="点歌 七里香",
        display_name="小张",
        metadata={"gift_name": "小心心", "price": 10},
    )
    is_req, query, rej = service.match_song_request(gift_ev)
    assert is_req is True
    assert query == "七里香"
    assert rej is None

    # Gift with insufficient coin and wrong name should be rejected
    cheap_gift = Event(
        type="gift",
        text="点歌 七里香",
        display_name="小王",
        metadata={"gift_name": "小花花", "price": 5},
    )
    is_req, query, rej = service.match_song_request(cheap_gift)
    assert is_req is True
    assert "点歌需要送出" in rej


def test_match_draw_and_guess_start(service):
    # Default is gift_only
    chat_ev = Event(type="chat", text="#你画我猜")
    should_start, _ = service.match_draw_and_guess_start(chat_ev)
    assert should_start is False

    # Gift matching name
    gift_ev = Event(type="gift", metadata={"gift_name": "画笔", "price": 10})
    should_start, _ = service.match_draw_and_guess_start(gift_ev)
    assert should_start is True

    # Gift matching min_coin
    gift_coin = Event(type="gift", metadata={"gift_name": "随机礼物", "price": 100})
    should_start, _ = service.match_draw_and_guess_start(gift_coin)
    assert should_start is True


def test_match_game_switch(service):
    # Danmaku command for PvZ
    chat_ev = Event(type="chat", text="#玩植物大战僵尸")
    gid, gname = service.match_game_switch(chat_ev)
    assert gid == "pvz"
    assert gname == "植物大战僵尸"

    # Gift for Stardew Valley
    gift_ev = Event(type="gift", metadata={"gift_name": "冰阔落", "price": 50})
    gid, gname = service.match_game_switch(gift_ev)
    assert gid == "stardew_valley"
    assert gname == "星露谷物语"


def test_match_game_action(service):
    # Gift asking to water crops in Stardew Valley
    gift_ev = Event(
        type="gift",
        text="大肥鱼快去给农作物浇水！",
        metadata={"gift_name": "小心心", "price": 10},
    )
    action, label = service.match_game_action(gift_ev, "stardew_valley")
    assert action == "water_crops"
    assert label == "浇水"

    # Gift asking for cherry bomb in PvZ
    pvz_gift = Event(
        type="gift",
        text="快放樱桃炸弹救场！",
        metadata={"gift_name": "炸弹", "price": 50},
    )
    action, label = service.match_game_action(pvz_gift, "pvz")
    assert action == "use_cherry_bomb"
    assert label == "放樱桃炸弹"
