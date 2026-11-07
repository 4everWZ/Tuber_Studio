import time

from test_browsing import BV
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


async def test_aggregated_failure_links_to_its_real_task_instead_of_its_display_identity(browsing):
    browsing.discovery.remember_account("100")
    row, _ = browsing.repo.create_round(
        "100", browsing.runtime.character, browsing.policy() | {"autonomous_batch": True}, 0, False,
    )
    browsing.repo.batches.ensure(row)
    candidate = {"bvid": BV, "title": "盐水实验"}
    browsing.repo.batches.merge(row, [candidate])
    task_id = browsing.repo.task(row["id"], candidate)
    browsing.repo.batches.outcome(task_id, "checkpoint", "正文与近期发言重复")
    issue = browsing.discovery.records()["issues"][0]
    assert issue["id"].startswith("failure-")
    assert issue["href"] == f"#automation/round/{row['id']}/{task_id}"
    assert browsing.tasks(task_id=task_id)[0]["bvid"] == BV
    # Old imported failures can have no retained task. Link to their round,
    # rather than constructing a nonexistent synthetic task destination.
    browsing.store.execute("UPDATE browsing_batch_items SET task_id=NULL WHERE batch_id=?", (row["config"]["batch_id"],))
    issue = browsing.discovery.records()["issues"][0]
    assert issue["href"] == f"#automation/round/{row['id']}"
