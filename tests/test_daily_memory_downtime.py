import datetime
import time

from tuber_studio.daily_memory import DailyMemoryService


def test_downtime_detection_and_highlight(store):
    service = DailyMemoryService(store)
    char = store.character()
    char_id = char["id"]
    char_version = char["version"]

    now = 1700000000.0

    # 1. First run: system_last_active_at was None, so it initializes and returns None
    result = service.check_and_record_downtime(char_id, char_version, current_time=now)
    assert result is None
    assert store.get("system_last_active_at") == now

    # 2. Short offline (e.g. 2 hours = 7200s) -> No downtime recorded
    result2 = service.check_and_record_downtime(char_id, char_version, current_time=now + 7200)
    assert result2 is None

    # 3. Offline for 3 days (3 * 86400 = 259200s)
    future = now + 7200 + 259200
    res3 = service.check_and_record_downtime(char_id, char_version, current_time=future)
    assert res3 is not None
    assert res3["days"] == 3
    assert "开机停工3天吐槽" in res3["headline"]
    assert "睡了好几天" in res3["anecdote"]
    assert "小窝太简陋了" in res3["anecdote"]

    # Verify highlight was recorded in database with valid source_evidence
    row = store.one("SELECT * FROM daily_highlights WHERE id=?", (res3["id"],))
    assert row is not None
    assert row["category"] == "community_meme"
    assert row["status"] == "active"
    assert service._source_evidence(row) is not None

    # 4. Anecdote retrieval with matching query
    anecdotes = service.get_active_anecdotes(
        char_id,
        char_version,
        query_text="大肥鱼这几天去哪了？怎么才开机",
        days=7,
    )
    assert len(anecdotes) >= 1
    matched = any("睡了好几天" in a["memory"] for a in anecdotes)
    assert matched

    # 5. Immediate re-check does not duplicate
    res4 = service.check_and_record_downtime(char_id, char_version, current_time=future + 10)
    assert res4 is None
