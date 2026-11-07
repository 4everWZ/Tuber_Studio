import pytest

from tuber_studio.maintenance import archive_character_version, prune_empty_interrupted_rounds
from tuber_studio.operations.browsing.repository import BrowsingRepository


def test_archived_card_is_hidden_but_keeps_historical_provenance(store):
    old = store.character("whale")
    current = store.save_character(old)
    archive_character_version(store, "whale", old["version"], "旧版人设")
    assert store.character("whale", old["version"]) == old
    assert [item["version"] for item in store.versions("whale")] == [current["version"]]
    with pytest.raises(ValueError, match="归档"):
        store.activate("whale", old["version"])
    with pytest.raises(ValueError, match="当前"):
        archive_character_version(store, "whale", current["version"], "当前版本")


def test_cleanup_removes_only_empty_interrupted_rounds(store):
    BrowsingRepository(store)
    for identity in ("empty", "materials", "replies", "recoverable", "batch"):
        store.execute(
            "INSERT INTO browsing_rounds(id,status,config) VALUES(?,'cancelled',?)",
            (identity, '{"batch_id":"kept"}' if identity == "batch" else "{}"),
        )
    store.execute(
        "INSERT INTO browsing_tasks(id,round_id,status,material_key,payload) "
        "VALUES('t','materials','cancelled','real-material','{}')"
    )
    store.execute(
        "INSERT INTO browsing_tasks(id,round_id,status,payload) VALUES('q','recoverable','checkpoint','{}')"
    )
    store.execute("INSERT INTO browsing_intents(id,round_id,status) VALUES('reply','replies','published')")
    assert prune_empty_interrupted_rounds(store) == ["empty"]
    assert len(store.all("SELECT id FROM browsing_rounds")) == 4
    assert prune_empty_interrupted_rounds(store) == []
