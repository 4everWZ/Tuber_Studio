"""Tests for Mount & Blade Warband style bidirectional [-100, +100] affinity and tiered storage."""

from tuber_studio.persona.memory import relationships as relationships
from tuber_studio.persona.memory.relationships import (
    Relationships,
    detect_negative_sentiment,
    detect_positive_sentiment,
)


def test_detect_negative_sentiment():
    assert detect_negative_sentiment("主播真下饭，好菜啊") == -12
    assert detect_negative_sentiment("真无聊，退订了，关播吧") == -12
    assert detect_negative_sentiment("死肥鱼，弱智主播真恶心死了") == -30
    assert detect_negative_sentiment("今天大肥鱼唱得太棒啦！") == 0
    assert detect_negative_sentiment("晚上吃什么呀？") == 0
    assert detect_positive_sentiment("晚上吃什么呀？") == 5
    assert detect_positive_sentiment("小鲸鱼真可爱") == 15
    assert detect_positive_sentiment("超喜欢小鲸鱼！今天这期视频科普讲得真棒，一直支持你做下去！") == 25


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
    assert "大肥鱼" in Relationships.tone_hint(0)
    assert "Aqua" in Relationships.tone_hint(0)
    assert "小鲸鱼" not in Relationships.tone_hint(0)
    assert "不要对号入座" in Relationships.tone_hint(0)
    assert "宿主" not in Relationships.tone_hint(90)


def test_negative_affinity_reduction_and_clamping(store, monkeypatch):
    relations = Relationships(store)
    character = store.character()
    args = (character["id"], "bilibili", "user_hostile_1", "黑粉头子")

    now = [1_000_000.]
    monkeypatch.setattr(relationships.time, "time", lambda: now[0])
    assert relations.view(*args)["affinity"] == 0
    assert relations.view(*args)["tone_hint"]

    # Mild negative remark (-12)
    view1 = relations.observe(*args, "comment:h1", "主播真菜，别叫了", account_id="900")
    assert view1["affinity"] == -12

    # Severe toxic remark inside 10s cooldown -> suppressed, then applied after 11s (-30)
    assert relations.observe(*args, "comment:h2", "垃圾主播去死吧弱智", account_id="900")["affinity"] == -12
    now[0] += 11
    view2 = relations.observe(*args, "comment:h2-later", "今天只想让你去死", account_id="900")
    assert view2["affinity"] == -42

    # Explicit large negative delta clamping at -100
    view3 = relations.observe(*args, "comment:h3", "不可饶恕", account_id="900", delta=-150)
    assert view3["affinity"] == -100
    assert view3["stage"] == "宿敌"

    # Explicit large positive delta clamping at +100
    view4 = relations.observe(*args, "comment:h4", "弃暗投明，超大红包", account_id="900", delta=300)
    assert view4["affinity"] == 100
    assert view4["stage"] == "特别熟悉"


def test_tiered_storage_transient_vs_persisted(store, monkeypatch):
    relations = Relationships(store)
    character_id = store.character()["id"]
    scope = ("s1", character_id, "bilibili")
    args = (*scope, "viewer")
    now = [1_000_000.]
    monkeypatch.setattr(relationships.time, "time", lambda: now[0])
    assert relations.observe_live(*args, "鱼宝", "live:1", "今晚吃啥啊", account_id="900")["affinity"] == 5
    assert relations.live_view(*args, account_id="900")["affinity"] == 5
    assert relations.live_view(*args, account_id="901")["affinity"] == 0
    assert relations.view(character_id, "bilibili", "viewer", account_id="900")["affinity"] == 0
    assert not store.all("SELECT * FROM account_relations")
    assert not store.all("SELECT * FROM users")
    now[0] += 11
    assert relations.observe_live(*args, "鱼宝", "live:1", "今晚吃啥啊", account_id="900")["affinity"] == 5
    assert relations.observe_live(*args, "鱼宝", "live:2", "花了100", account_id="900", support=True)["affinity"] == 5
    relations.clear_live()
    assert relations.live_view("s2", *args[1:], account_id="900")["affinity"] == 5
    assert relations.view(character_id, "bilibili", "viewer", account_id="900")["affinity"] == 5
    assert len(store.all("SELECT * FROM account_relation_events")) == 1


def test_transient_lru_ttl_and_signed_bounds(store, monkeypatch):
    relations = Relationships(store)
    scope = ("s1", store.character()["id"], "bilibili")
    now = [1_000_000.]
    monkeypatch.setattr(relationships.time, "time", lambda: now[0])
    monkeypatch.setattr(relationships, "LIVE_SCORE_LIMIT", 2)
    for uid in ("u1", "u2"):
        relations.observe_live(*scope, uid, "同名", "live:1", "去死", account_id="900", delta=-60)
    assert relations.live_view(*scope, "u1", account_id="900")["affinity"] == -50
    relations.observe_live(*scope, "u3", "同名", "live:1", "打招呼啦", account_id="900", delta=60)
    assert relations.live_view(*scope, "u2", account_id="900")["affinity"] == 0
    assert relations.live_view(*scope, "u3", account_id="900")["affinity"] == 50
    now[0] += 11
    assert relations.observe_live(*scope, "u1", "同名", "live:2", "滚出去", account_id="900", delta=-5)["affinity"] == -50
    assert relations.observe_live(*scope, "u1", "同名", "live:3", "正常聊聊天", account_id="900", delta=10)["affinity"] == -40
    now[0] += relationships.LIVE_SCORE_TTL
    assert relations.live_view(*scope, "u1", account_id="900")["affinity"] == 0
    assert not store.all("SELECT * FROM account_relations")
