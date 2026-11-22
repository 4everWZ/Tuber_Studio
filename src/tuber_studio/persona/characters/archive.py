"""Character version archival while preserving historical provenance."""

import time


def archive_character_version(store, character_id, version, reason):
    """Hide retired cards from selection; historical provenance stays readable."""
    store.character(character_id, version)
    if store.get("active_character") == character_id:
        active = store.character(character_id)
        if active["version"] == version:
            raise ValueError("当前使用中的人设不能归档")
    store.execute(
        "INSERT OR IGNORE INTO character_archives VALUES(?,?,?,?)",
        (character_id, version, reason, time.time()),
    )
