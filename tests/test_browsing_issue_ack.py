import time

from test_browsing import browsing as browsing

from tuber_studio.operations.browsing.issues import clear_issues
from tuber_studio.store import dump


def add_error(browsing, identity, stamp):
    browsing.store.execute(
        "INSERT INTO browsing_rounds(id,account_id,character_id,character_version,status,config,metrics,error,created_at,updated_at) "
        "VALUES(?,'100','whale',?,'failed',?,'{}','读取失败',?,?)",
        (
            identity,
            browsing.runtime.character["version"],
            dump(browsing.policy() | {"discovery_publish": True}),
            stamp,
            stamp,
        ),
    )


async def test_clear_acknowledges_existing_errors_and_new_occurrences_still_appear(browsing):
    browsing.discovery.remember_account("100")
    now = time.time()
    add_error(browsing, "old", now - 5)
    assert len(browsing.discovery.records()["issues"]) == 1
    assert clear_issues(browsing)["issues"] == []
    assert browsing.discovery.records()["issues"] == []
    assert browsing.store.one("SELECT error FROM browsing_rounds WHERE id='old'")["error"] == "读取失败"
    add_error(browsing, "new", now + 5)
    issue = browsing.discovery.records()["issues"][0]
    assert issue["occurrences"] == 1
    assert issue["error"] == "读取失败"


async def test_another_occurrence_in_the_same_clock_tick_remains_unread(browsing, monkeypatch):
    browsing.discovery.remember_account("100")
    clock = time.time()
    monkeypatch.setattr("tuber_studio.operations.browsing.issues.time.time", lambda: clock)
    add_error(browsing, "first", clock)
    assert clear_issues(browsing)["issues"] == []
    add_error(browsing, "second", clock)
    issue = browsing.discovery.records()["issues"][0]
    assert issue["occurrences"] == 1
    assert clear_issues(browsing)["issues"] == []
    add_error(browsing, "third", clock)
    assert browsing.discovery.records()["issues"][0]["occurrences"] == 1


async def test_acknowledgement_counts_restart_with_the_new_version_window(browsing, monkeypatch):
    browsing.discovery.remember_account("100")
    clock = [time.time()]
    monkeypatch.setattr("tuber_studio.operations.browsing.issues.time.time", lambda: clock[0])
    for number in range(5):
        add_error(browsing, f"old-{number}", clock[0])
    assert clear_issues(browsing)["issues"] == []
    browsing.store.save_character(browsing.runtime.character)
    clock[0] += 5
    add_error(browsing, "new-first", clock[0])
    assert browsing.discovery.records()["issues"][0]["occurrences"] == 1
    assert clear_issues(browsing)["issues"] == []
    clock[0] += 1
    add_error(browsing, "new-second", clock[0])
    add_error(browsing, "new-third", clock[0])
    assert browsing.discovery.records()["issues"][0]["occurrences"] == 2
