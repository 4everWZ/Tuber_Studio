"""Rejected submissions can be revised without opening a duplicate-send path."""

import concurrent.futures
import copy

import pytest

from tuber_studio.operations_control import OperationsControl, canonical_intent
from tuber_studio.store import Store


def scope(character, source="42"):
    return canonical_intent("bilibili", "BV1d8pU6xEZX", source, character["id"])


def binding(source="42"):
    return {"target": {"resource": "BV1d8pU6xEZX", "parent": source, "recipient": "123"}, "source": source}


def rejected(control, character, source="42"):
    token = control.reserve(
        "900", character, scope(character, source), "comments", recipient="123", binding=binding(source)
    )
    control.finish(token, "rejected", error="platform rejected length")
    return token


def revise(control, character, ticket, **changes):
    values = dict(recipient="123", binding=binding(), revision_ticket=ticket, content_hash="a" * 64)
    values.update(changes)
    return control.reserve("900", character, scope(character), "comments", **values)


def test_revision_keeps_root_unique_and_every_attempt_immutable(store):
    control, character = OperationsControl(store), store.character()
    first = rejected(control, character)
    ticket = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    second = revise(control, character, ticket)
    control.finish(second, "published", receipt={"rpid": "7"})
    assert store.one("SELECT COUNT(*) AS n FROM account_writes")["n"] == 1
    assert store.one("SELECT status FROM account_writes WHERE token=?", (first,))["status"] == "rejected"
    assert control.get_attempt(second)["previous_token"] == first
    assert control.get_attempt(second)["root_token"] == first
    assert control.get_attempt(second)["resolved_status"] == "published"
    assert control.get_attempt(first)["binding"] == binding()
    assert control.get_attempt(second)["binding"] == binding()
    with pytest.raises(ValueError, match="禁止重复"):
        control.reserve("900", character, scope(character), "comments")
    with pytest.raises(ValueError):
        revise(control, character, ticket)
    with pytest.raises(ValueError):
        control.finish(first, "published")


def test_attempt_binding_is_a_fresh_decoded_snapshot_and_legacy_stays_unbound(store):
    control, character = OperationsControl(store), store.character()
    first = rejected(control, character)
    snapshot = control.get_attempt(first)
    snapshot["binding"]["target"]["parent"] = "changed-by-caller"
    assert control.get_attempt(first)["binding"] == binding()
    legacy = control.reserve("900", character, scope(character, "legacy-unbound"), "comments")
    assert control.get_attempt(legacy)["binding"] is None
    assert control.get_attempt("missing-attempt") is None


@pytest.mark.parametrize("status", ["published", "uncertain", "cancelled", "sending"])
def test_only_definite_rejection_can_authorize_revision(store, status):
    control, character = OperationsControl(store), store.character()
    first = control.reserve("900", character, scope(character), "comments", binding=binding())
    if status != "sending":
        control.finish(first, status)
    with pytest.raises(ValueError, match="明确拒绝"):
        control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)


def test_ticket_binds_original_target_source_and_actual_revised_body(store):
    control, character = OperationsControl(store), store.character()
    first = rejected(control, character)
    ticket = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    modified = copy.deepcopy(binding())
    modified["target"]["parent"] = "other"
    for patch in ({"binding": modified}, {"recipient": "other"}, {"content_hash": "b" * 64}):
        with pytest.raises(ValueError):
            revise(control, character, ticket, **patch)
    assert len(store.all("SELECT * FROM operation_write_attempts")) == 1
    second = revise(control, character, ticket)
    control.finish(second, "rejected")
    with pytest.raises(ValueError, match="后续尝试"):
        control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)


def test_epoch_change_revokes_old_ticket_but_new_authorized_round_can_continue(store):
    control, character = OperationsControl(store), store.character()
    first = rejected(control, character)
    old = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    control.configure({"max_per_hour": 20})
    with pytest.raises(ValueError, match="权限版本"):
        revise(control, character, old)
    current = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    assert revise(control, character, current)


def test_retry_tickets_are_atomic_between_real_sqlite_connections(store, settings):
    control, character = OperationsControl(store), store.character()
    first = rejected(control, character)
    second_store = Store(settings.runtime / "test.sqlite3", [character])
    other = OperationsControl(second_store)
    ticket = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)

    def attempt(service):
        try:
            return revise(service, character, ticket)
        except ValueError:
            return None

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            tokens = list(executor.map(attempt, [control, other]))
        assert len([token for token in tokens if token]) == 1
        assert len(store.all("SELECT * FROM operation_write_retries")) == 1
    finally:
        second_store.close()


def test_all_attempts_count_toward_quota_but_rejection_does_not_fake_recipient_delivery(store):
    control, character = OperationsControl(store), store.character()
    control.configure({"max_per_hour": 2})
    first = rejected(control, character)
    ticket = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    second = revise(control, character, ticket)
    control.finish(second, "rejected")
    with pytest.raises(ValueError, match="全渠道"):
        control.reserve("900", character, scope(character, "43"), "comments")
    assert sum(row["count"] for row in control.status()["attempts"]) == 2


def test_restart_marks_retry_uncertain_and_positive_readback_resolves_without_resending(store):
    control, character = OperationsControl(store), store.character()
    first = rejected(control, character)
    ticket = control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    second = revise(control, character, ticket)
    reopened = OperationsControl(store)
    assert reopened.get_attempt(second)["status"] == "uncertain"
    with pytest.raises(ValueError):
        reopened.issue_revision_ticket(second, binding=binding(), content_hash="b" * 64)
    result = reopened.reconcile_attempt(second, account_id="900", binding=binding(), receipt={"rpid": "7"})
    assert result["status"] == "uncertain" and result["resolved_status"] == "published"
    assert not reopened.status()["channel_blocks"]
    assert len(store.all("SELECT * FROM operation_write_attempts")) == 2
    reopened.reconcile_attempt(second, account_id="900", binding=binding(), receipt={"rpid": "7"})
    with pytest.raises(ValueError, match="不能改写"):
        reopened.reconcile_attempt(second, account_id="900", binding=binding(), receipt={"rpid": "8"})


def test_readback_does_not_clear_another_unresolved_write_or_accept_wrong_binding(store):
    control, character = OperationsControl(store), store.character()
    tokens = []
    for source in ("42", "43"):
        token = control.reserve(
            "900", character, scope(character, source), "comments", binding=binding(source)
        )
        control.finish(token, "uncertain")
        tokens.append(token)
    with pytest.raises(ValueError):
        control.reconcile_attempt(tokens[0], account_id="901", binding=binding(), receipt={"rpid": "7"})
    with pytest.raises(ValueError):
        control.reconcile_attempt(tokens[0], account_id="900", binding=binding("43"), receipt={"rpid": "7"})
    control.reconcile_attempt(tokens[0], account_id="900", binding=binding(), receipt={"rpid": "7"})
    assert control.status()["channel_blocks"]


def test_legacy_unbound_rejection_is_preserved_and_not_given_a_guessed_target(store):
    control, character = OperationsControl(store), store.character()
    first = control.reserve("900", character, scope(character), "comments")
    control.finish(first, "rejected")
    with pytest.raises(ValueError, match="重新准备"):
        control.issue_revision_ticket(first, binding=binding(), content_hash="a" * 64)
    assert control.get_attempt(first)["status"] == "rejected"
