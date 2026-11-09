"""Tests for Mount & Blade Warband style bidirectional [-100, +100] affinity and tiered storage."""

import pytest
from tuber_studio.relationships import Relationships, detect_negative_sentiment


def test_detect_negative_sentiment():
    assert detect_negative_sentiment("主播真下饭，好菜啊") == -2
    assert detect_negative_sentiment("真无聊，退订了，关播吧") == -2
    assert detect_negative_sentiment("死肥鱼，弱智主播真恶心死了") == -10
    assert detect_negative_sentiment("今天大肥鱼唱得太棒啦！") == 0
    assert detect_negative_sentiment("晚上吃什么呀？") == 0


def test_warband_affinity_stages():
    assert Relationships.stage(-100) == "宿敌"
    assert Relationships.stage(-60) == "宿敌"
    assert Relationships.stage(-59) == "恶感"
    assert Relationships.stage(-20) == "恶感"
    assert Relationships.stage(-19) == "陌生"
    assert Relationships.stage(0) == "陌生"
    assert Relationships.stage(20) == "陌生"
    assert Relationships.stage(30) == "认识"
    assert Relationships.stage(59) == "认识"
    assert Relationships.stage(60) == "熟悉"
    assert Relationships.stage(89) == "熟悉"
    assert Relationships.stage(90) == "特别熟悉"
    assert Relationships.stage(100) == "特别熟悉"


def test_negative_affinity_reduction_and_clamping(store):
    relations = Relationships(store)
    character = store.character()
    args = (character["id"], "bilibili", "user_hostile_1", "黑粉头子")

    # Initial baseline is 20
    assert relations.view(*args)["affinity"] == 20

    # Mild negative remark
    view1 = relations.observe(*args, "comment:h1", "主播真菜，别叫了", account_id="900")
    assert view1["affinity"] == 18

    # Severe toxic remark
    view2 = relations.observe(*args, "comment:h2", "垃圾主播去死吧弱智", account_id="900")
    assert view2["affinity"] == 8

    # Explicit large negative delta clamping at -100
    view3 = relations.observe(*args, "comment:h3", "不可饶恕", account_id="900", delta=-150)
    assert view3["affinity"] == -100
    assert view3["stage"] == "宿敌"

    # Explicit large positive delta clamping at +100
    view4 = relations.observe(*args, "comment:h4", "弃暗投明，超大红包", account_id="900", delta=300)
    assert view4["affinity"] == 100
    assert view4["stage"] == "特别熟悉"


def test_tiered_storage_transient_vs_persisted(store):
    relations = Relationships(store)
    character = store.character()
    
    # 1. Transient live chat with neutral remark (delta = 1, affinity goes 20 -> 21, |21-20| < 10)
    # Should stay in _ephemeral_cache and NOT write to account_relations table
    args_passerby = (character["id"], "bilibili", "passerby_999", "路人甲")
    view_transient = relations.observe(*args_passerby, "live:room1:evt1", "大肥鱼今晚吃啥", account_id="900")
    assert view_transient["affinity"] == 21

    # Verify NOT in SQLite table
    db_rows = store.all(
        "SELECT * FROM account_relations WHERE account_id='900' AND character_id=?",
        (character["id"],),
    )
    user_ids_in_db = [r["user_id"] for r in db_rows]
    passerby_user = store.one("SELECT id FROM users WHERE platform='bilibili' AND platform_user_id='passerby_999'")
    assert passerby_user["id"] not in user_ids_in_db

    # But view still works via ephemeral cache!
    assert relations.view(*args_passerby, account_id="900")["affinity"] == 21

    # 2. Paid user in live stream (is_paid=True) -> Must persist to SQLite
    args_vip = (character["id"], "bilibili", "vip_888", "舰长大人")
    view_vip = relations.observe(*args_vip, "live:room1:gift1", "送了一艘大航海", account_id="900", is_paid=True, delta=10)
    assert view_vip["affinity"] == 30

    vip_user = store.one("SELECT id FROM users WHERE platform='bilibili' AND platform_user_id='vip_888'")
    vip_db_row = store.one(
        "SELECT * FROM account_relations WHERE account_id='900' AND character_id=? AND user_id=?",
        (character["id"], vip_user["id"]),
    )
    assert vip_db_row is not None

    # 3. Severe troll in live stream (|affinity - 20| >= 10) -> Promoted to persistent SQLite for accountability!
    args_troll = (character["id"], "bilibili", "troll_666", "恶意黑子")
    view_troll = relations.observe(*args_troll, "live:room1:troll1", "死肥鱼弱智主播去死", account_id="900")
    assert view_troll["affinity"] == 10  # 20 - 10 = 10, abs(10-20) == 10 >= 10
    troll_user = store.one("SELECT id FROM users WHERE platform='bilibili' AND platform_user_id='troll_666'")
    troll_db_row = store.one(
        "SELECT * FROM account_relations WHERE account_id='900' AND character_id=? AND user_id=?",
        (character["id"], troll_user["id"]),
    )
    assert troll_db_row is not None
