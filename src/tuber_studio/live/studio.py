"""Operator-only views of the current room and its playback history."""


def room_feed(live):
    store = live.store
    input_type = "coalesce(json_extract(payload,'$.metadata._live_input.type'), json_extract(payload,'$.type'))"
    chat = store.all(
        f"SELECT id, {input_type} AS type, json_extract(payload,'$.display_name') AS requester, "
        "coalesce(json_extract(payload,'$.metadata._live_input.text'), json_extract(payload,'$.text')) AS text, "
        "status, reason, created_at AS received_at FROM events WHERE session_id=? "
        f"AND {input_type} IN ('chat','gift') "
        "ORDER BY id DESC LIMIT 50",
        (live.session_id,),
    )
    projection = (
        "SELECT j.id, j.event_id, json_extract(e.payload,'$.type') AS type, "
        "json_extract(e.payload,'$.display_name') AS requester, j.status, "
        "CASE WHEN j.status='completed' THEN j.text END AS text, "
        "CASE WHEN j.status IN ('failed','cancelled','expired') THEN j.error END AS error, j.created_at "
        "FROM jobs j JOIN events e ON j.event_id=e.id WHERE j.session_id=? "
    )
    dialogue = store.all(
        projection + "AND json_extract(e.payload,'$.type') <> 'song' ORDER BY j.created_at DESC LIMIT 20",
        (live.session_id,),
    )
    current = store.one(projection + "AND j.id=?", (live.session_id, live.current_id))
    if current and current["type"] == "song":
        current["song"] = store.one(
            "SELECT json_extract(media,'$.song.title') AS title, json_extract(media,'$.song.artist') AS artist, "
            "json_extract(media,'$.mode') AS mode FROM jobs WHERE id=?",
            (live.current_id,),
        )
    last_spoken = store.one(
        projection + "AND j.status='completed' AND json_extract(e.payload,'$.type') <> 'song' "
        "ORDER BY j.updated_at DESC LIMIT 1",
        (live.session_id,),
    )
    unresolved = store.one(
        "SELECT error FROM jobs WHERE session_id=? AND status='failed' AND updated_at > coalesce("
        "(SELECT max(updated_at) FROM jobs WHERE session_id=? AND status='completed'),0) "
        "ORDER BY updated_at DESC LIMIT 1",
        (live.session_id, live.session_id),
    )
    return {
        "session_id": live.session_id,
        "chat": list(reversed(chat)),
        "dialogue": dialogue,
        "current": current,
        "last_spoken": last_spoken,
        "timings": live.timings,
        "error": live.runtime.error if live.state == "degraded" else (unresolved or {}).get("error"),
    }
