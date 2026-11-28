import json
import sqlite3
import time

import pytest

from tuber_studio.live.games.roast_radio import RoastRadioService
from tuber_studio.live.games.turtle_soup import PUZZLE_BANK, TurtleSoupService
from tuber_studio.live.session.cold_director import ColdRoomDirector


class _DictStore:
    """Minimal SQLite store matching Store.all(...) for isolated public-data tests."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row

    def all(self, sql: str, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]


class _MockLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def generate_json(self, messages, schema, *, max_tokens: int):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _StubDrawAndGuess:
    def __init__(self):
        self.rounds = []
        self._active = False

    def is_active(self):
        return self._active

    async def start_round_llm(self, llm, *, trigger_user="观众", mode="aqua_draws"):
        self._active = True
        payload = {"status": "started", "trigger_user": trigger_user, "mode": mode, "spoken_text": "涂鸦开局！"}
        self.rounds.append(payload)
        return payload


# ---------------------------------------------------------------------------
# TurtleSoupService
# ---------------------------------------------------------------------------


def test_turtle_soup_fallback_bank_and_sync_flow():
    assert len(PUZZLE_BANK) >= 4
    for p in PUZZLE_BANK:
        assert p["surface"] and p["answer"]
        assert len(p["hints"]) >= 2

    svc = TurtleSoupService()
    assert svc.is_active() is False

    started = svc.start_round(trigger_user="房管", genre="sci-fi")
    assert svc.is_active() is True
    assert started["genre"] == "sci-fi"
    assert started["trigger_user"] == "房管"
    assert started["surface"] in started["spoken_text"]

    # Answer must NOT be exposed before finishing
    overlay_mid = svc.get_overlay_state()
    assert overlay_mid["active"] is True
    assert overlay_mid["state"] == "playing"
    assert overlay_mid["answer"] is None

    hint = svc.give_hint()
    assert hint["hint"]
    assert hint["hint_index"] == 1

    revealed = svc.reveal_and_stop(winner="阿瓜粉丝")
    assert svc.is_active() is False
    assert revealed["answer"]
    overlay_done = svc.get_overlay_state()
    assert overlay_done["active"] is False
    assert overlay_done["state"] == "finished"
    assert overlay_done["winner"] == "阿瓜粉丝"
    assert overlay_done["answer"] == revealed["answer"]


async def test_turtle_soup_llm_and_danmaku_judgement():
    svc = TurtleSoupService()
    llm = _MockLLM(
        [
            {
                "title": "时间胶囊",
                "genre": "mystery",
                "surface": "他打开十年前埋下的铁盒，看到里面的照片后立刻报警了。为什么？",
                "answer": "照片里拍到了昨天才建好的大楼。",
                "key_clues": ["照片的时间与铁盒埋下时间矛盾", "背景建筑最近才建成"],
                "hints": ["注意照片背景里出现了什么东西。", "十年前不该存在的建筑。"],
                "yes_keywords": ["建筑", "大楼", "时间", "矛盾"],
                "no_keywords": ["鬼", "血"],
                "solve_keywords": ["照片", "昨天", "新建", "大楼"],
            },
            # Unrelated chatter -> ignored
            {"kind": "unrelated", "verdict": "irrelevant", "spoken_text": ""},
            # Yes question with clue unlock
            {
                "kind": "question",
                "verdict": "yes",
                "unlocked_clue": "照片背景建筑最近才建成",
                "spoken_text": "对！眼神很尖嘛。",
            },
            # Direct solve
            {
                "kind": "guess",
                "verdict": "solved",
                "spoken_text": "恭喜破案！照片里拍到了昨天才建好的大楼。",
            },
        ]
    )

    started = await svc.start_round_llm(llm, trigger_user="小明", genre="mystery")
    assert started["llm_generated"] is True
    assert svc.is_active() is True

    ignored = await svc.handle_danmaku(llm, user_id="u1", display_name="路人", text="晚饭吃白饭了吗")
    assert ignored is None
    assert svc.is_active() is True

    yes_res = await svc.handle_danmaku(llm, user_id="u2", display_name="侦探A", text="照片里的建筑有问题吗？")
    assert yes_res is not None
    assert yes_res["kind"] == "turtle_soup_qa"
    assert yes_res["verdict"] == "yes"
    assert "照片背景建筑最近才建成" in yes_res["clues_found"]

    solved = await svc.handle_danmaku(llm, user_id="u3", display_name="侦探B", text="我猜照片里拍到了新建的大楼！")
    assert solved is not None
    assert solved["kind"] == "turtle_soup_solved"
    assert solved["verdict"] == "solved"
    assert solved["winner"] == "侦探B"
    assert svc.is_active() is False


async def test_turtle_soup_rule_fallback_when_llm_fails():
    svc = TurtleSoupService()
    svc.start_round(trigger_user="主播", genre="daily")
    # Force known puzzle for deterministic rule matching
    svc.current_puzzle = dict(PUZZLE_BANK[0])  # midnight_elevator

    failing_llm = _MockLLM([RuntimeError("timeout"), RuntimeError("timeout"), RuntimeError("timeout")])

    assert await svc.handle_danmaku(failing_llm, user_id="u1", display_name="A", text="晚上好哈哈") is None
    q_res = await svc.handle_danmaku(failing_llm, user_id="u2", display_name="B", text="是不是和身高有关？")
    assert q_res is not None
    assert q_res["verdict"] == "yes"

    solve_res = await svc.handle_danmaku(
        failing_llm, user_id="u3", display_name="C", text="我猜是他太矮够不到按钮，雨天能用雨伞按！"
    )
    assert solve_res is not None
    assert solve_res["kind"] == "turtle_soup_solved"
    assert solve_res["winner"] == "C"

    # qa_history capped at last 6 in overlay
    svc.start_round(trigger_user="主播")
    for idx in range(8):
        await svc.handle_danmaku(None, user_id=f"u{idx}", display_name=f"V{idx}", text="是不是鬼故事？")
    assert len(svc.get_overlay_state()["qa_history"]) == 6


# ---------------------------------------------------------------------------
# RoastRadioService & strict public-only isolation
# ---------------------------------------------------------------------------


def test_roast_radio_strictly_public_only_isolation():
    store = _DictStore()
    store.db.executescript(
        """
        CREATE TABLE browsing_video_progress(
            account_id TEXT, character_id TEXT, bvid TEXT, material TEXT, decisions TEXT, updated_at REAL
        );
        CREATE TABLE memories(
            id TEXT PRIMARY KEY, scope TEXT, character_id TEXT, content TEXT,
            source TEXT, source_key TEXT, tags TEXT, created_at REAL, updated_at REAL, active INTEGER
        );
        CREATE TABLE dm_inbox(id TEXT PRIMARY KEY, content TEXT);
        CREATE TABLE operator_chat_turns(id TEXT PRIMARY KEY, content TEXT);
        """
    )
    now = time.time()
    # Populate private tables and owner memories with obvious sentinel secrets
    store.db.execute("INSERT INTO dm_inbox VALUES('dm1', 'PRIVATE_DM_SECRET_123')")
    store.db.execute("INSERT INTO operator_chat_turns VALUES('op1', 'OWNER_CHAT_SECRET_456')")
    store.db.execute(
        "INSERT INTO memories VALUES('m_owner', 'owner', 'whale', 'OWNER_PRIVATE_MEMORY_789', 'manual', '', '[]', ?, ?, 1)",
        (now + 100, now + 100),
    )
    store.db.execute(
        "INSERT INTO memories VALUES('m_pub', 'public', 'whale', '公开趣闻：半夜电饭煲跳闸最香', 'manual', '', '[\"夜宵\"]', ?, ?, 1)",
        (now, now),
    )
    store.db.execute(
        "INSERT INTO browsing_video_progress VALUES('acc1', 'whale', 'BV1PUBLIC001', ?, '[]', ?)",
        (json.dumps({"metadata": {"title": "开源机械键盘评测", "tags": ["键盘"], "description": "公开视频摘要"}}), now + 10),
    )
    store.db.commit()

    radio = RoastRadioService()
    first = radio.pick_public_watch_topic(store)
    assert first is not None
    assert first["source_bvid"] == "BV1PUBLIC001"
    assert "开源机械键盘评测" in first["topic"]
    radio.recent_topics.append(first["topic"])

    second = radio.pick_public_watch_topic(store)
    assert second is not None
    assert "公开趣闻" in second["topic"]

    # Remove public rows: must return None rather than leaking owner/dm/operator data
    store.db.execute("DELETE FROM browsing_video_progress")
    store.db.execute("DELETE FROM memories WHERE scope='public'")
    store.db.commit()
    radio.recent_topics.clear()
    assert radio.pick_public_watch_topic(store) is None


async def test_roast_radio_debate_voting_and_roast_generation():
    radio = RoastRadioService()
    assert radio.is_active() is False

    debate = radio.start_debate(topic="白饭配老干妈还是腐乳", side_a="老干妈", side_b="腐乳", trigger_user="观众甲")
    assert radio.is_active() is True
    assert debate["mode"] == "debate"

    # Votes and per-user_id deduplication
    assert radio.record_vote(user_id="u1", display_name="A", text="1")["side"] == "a"
    assert radio.record_vote(user_id="u1", display_name="A", text="2") is None  # deduplicated
    assert radio.record_vote(user_id="u2", display_name="B", text="反方")["side"] == "b"
    assert radio.record_vote(user_id="u3", display_name="C", text="老干妈")["side"] == "a"
    assert radio.record_vote(user_id="u4", display_name="D", text="路过看看") is None

    overlay = radio.get_overlay_state()
    assert overlay["votes_a"] == 2
    assert overlay["votes_b"] == 1
    assert overlay["mode"] == "debate"

    roast_in_debate = await radio.generate_roast_segment(None, None)
    assert roast_in_debate["mode"] == "debate"
    assert roast_in_debate["spoken_text"]

    stopped = radio.stop()
    assert stopped["status"] == "stopped"
    assert radio.is_active() is False

    # Standalone public roast with LLM fallback
    llm = _MockLLM([{"spoken_text": "买五百块键帽只为了敲666，你们这性价比算盘打得太响了吧！"}])
    roast = await radio.generate_roast_segment(llm, None, topic="机械键盘只打弹幕")
    assert roast["mode"] == "roast"
    assert roast["source_bvid"] is None
    assert "键帽" in roast["spoken_text"]
    assert radio.is_active() is True


# ---------------------------------------------------------------------------
# ColdRoomDirector: 60-minute (0s -> 3600s) simulation + viewer wakeup
# ---------------------------------------------------------------------------


async def test_cold_director_60_minute_simulation_and_wakeup():
    t0 = 1000.0
    director = ColdRoomDirector(now=t0, tier3_cadence=300.0)
    draw = _StubDrawAndGuess()
    soup = TurtleSoupService()
    radio = RoastRadioService()

    # Tier 0: 0..89s -> no cold segment
    assert director.current_tier(t0 + 0) == 0
    assert director.current_tier(t0 + 89) == 0
    assert await director.next_segment(now=t0 + 60, draw_and_guess=draw, turtle_soup=soup, roast_radio=radio) is None

    # Tier 1: 90..299s -> warmup banter
    assert director.current_tier(t0 + 90) == 1
    seg_t1 = await director.next_segment(now=t0 + 95, draw_and_guess=draw, turtle_soup=soup, roast_radio=radio)
    assert seg_t1 is not None and seg_t1["tier"] == 1
    assert seg_t1["kind"] == "warmup_banter"
    # Respects cooldown within Tier 1
    assert await director.next_segment(now=t0 + 120) is None

    # Tier 2: 300..899s -> self-initiated interactive show rotation
    assert director.current_tier(t0 + 300) == 2
    t2_shows = []
    for ts in (t0 + 300, t0 + 460, t0 + 620):
        seg = await director.next_segment(
            now=ts, draw_and_guess=draw, turtle_soup=soup, roast_radio=radio
        )
        assert seg is not None and seg["tier"] == 2
        t2_shows.append(seg["show"])
    assert t2_shows == ["draw_and_guess", "roast_radio", "turtle_soup"]

    # Tier 3: 900s..3600s+ -> low-frequency cozy companion radio (240..360s cadence)
    assert director.current_tier(t0 + 900) == 3
    t3_times = []
    t3_segments = []
    # Step across 900s -> 3660s in 60s increments (simulating >60 minutes in milliseconds!)
    for elapsed in range(900, 3661, 60):
        seg = await director.next_segment(
            now=t0 + elapsed, draw_and_guess=draw, turtle_soup=soup, roast_radio=radio
        )
        if seg is not None:
            assert seg["tier"] == 3
            assert seg["kind"] == "companion_radio"
            # Zero begging / zero fake dying guard
            for forbidden in ("饿死", "求求", "没钱", "余额", "快死了"):
                assert forbidden not in seg["spoken_text"]
            t3_times.append(elapsed)
            t3_segments.append(seg)

    # At 300s cadence across 900..3660s -> fires at 900, 1200, ..., 3600 (10 times)
    assert len(t3_segments) == 10
    for prev_t, next_t in zip(t3_times, t3_times[1:]):
        assert 240 <= (next_t - prev_t) <= 360

    status_at_1h = director.status(t0 + 3600)
    assert status_at_1h["tier"] == 3
    assert status_at_1h["elapsed_seconds"] == 3600.0
    assert status_at_1h["total_segments"] == 14

    # Viewer activity wakes up from Tier 3 -> resets to Tier 0 and returns one-shot tsundere cue
    wakeup = director.on_viewer_activity(t0 + 3610)
    assert wakeup is not None
    assert wakeup["from_tier"] == 3
    assert wakeup["spoken_text"]
    assert director.current_tier(t0 + 3610) == 0

    # Subsequent activity in Tier 0 returns None
    assert director.on_viewer_activity(t0 + 3620) is None

    # Invalid tier3_cadence rejected
    with pytest.raises(ValueError):
        ColdRoomDirector(tier3_cadence=120)
