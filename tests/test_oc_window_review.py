from evaluations.oc_window_review import recovery, stats_owners


def test_stats_ownership_does_not_relabel_closed_or_ambiguous_legacy_samples():
    frames = [[0, 0, 1, 101, 1, 50, 1], [1000, 2, 2, 303, 2, 50, 2]]
    samples = [
        {"ms": 100, "segment": 2, "video": {"ssrc": 101}},
        {"ms": 200, "segment": 2, "state": "closed"},
        {"ms": 300, "segment": 2, "audio": {"ssrc": 50}},
        {"ms": 1100, "segment": 2, "video": {"ssrc": 303}, "audio": {"ssrc": 50}},
    ]
    assert [row["segment"] for row in stats_owners(samples, frames, False)] == [0, None, None, 2]
    assert [row["segment"] for row in stats_owners(samples, frames, True)] == [None, 2, 2, 2]


def test_archive_recovery_uses_frame_instead_of_element_state():
    cut = {"now": 1000, "segment": 2}
    frames = [[950, 0], [5085.5, 2]]
    assert recovery(cut, 6000, frames, None) == {
        "new_peer_play_ms": 4085.5,
        "presented_segment": 2,
        "peer_stages": None,
    }
    assert recovery(cut, 5000, frames, None)["new_peer_play_ms"] is None


def test_recovery_requires_the_same_attached_track_and_recorded_frame():
    cut = {"now": 1000, "segment": 0, "binding": 1}
    frames = [[990, 0], [1600, 2]]
    events = [
        {"ms": 1050, "stage": "peer_created", "segment": 1},
        {"ms": 1100, "stage": "peer_created", "segment": 2},
        {"ms": 1300, "stage": "video_attached", "segment": 2, "binding": 2, "track": "new"},
        {
            "ms": 1595,
            "stage": "first_presented",
            "displayMs": 1600,
            "segment": 2,
            "binding": 2,
            "track": "old",
        },
    ]
    assert recovery(cut, 2000, frames, events)["new_peer_play_ms"] is None
    events[-1]["track"] = "new"
    assert recovery(cut, 2000, frames[:1], events)["new_peer_play_ms"] is None
    result = recovery(cut, 2000, frames, events)
    assert result["new_peer_play_ms"] == 600
    assert result["peer_stages"] == [
        {"segment": 1, "created_ms": 50, "attachments": []},
        {
            "segment": 2,
            "created_ms": 100,
            "attachments": [{"binding": 2, "track": "new", "attached_ms": 300, "first_presented_ms": 600}],
        },
    ]
