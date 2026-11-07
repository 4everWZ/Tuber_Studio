"""Read-only DM cohort audit; stdout contains IDs/categories/counts only.

No model, network, runtime initialization or transport is called. Private bodies
are inspected in memory only for binding/content-presence checks and never dumped.
Semantic correctness is explicitly pending until an internal reviewer supplies a
content-free verdict for that exact draft revision. This does not simulate a send.
"""

import argparse
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from tuber_studio.message_outreach import (
    OutreachAssessment,
    invitation_anchor,
    invitation_key,
    outreach_sources,
    validate_outreach,
)


def load(value, default=None):
    return json.loads(value) if value else default


def completion_evidence(db, draft, evidence):
    result = []
    for proof in evidence.get("completion_evidence", []):
        if not isinstance(proof, dict) or not proof.get("step_id"):
            continue
        saved = db.execute(
            "SELECT s.id,s.phase,s.status,c.binding FROM operation_continuation_steps s "
            "JOIN operation_continuations c ON c.id=s.continuation_id WHERE s.id=?", (proof["step_id"],)
        ).fetchone()
        binding = load(saved["binding"], {}) if saved else {}
        actor = binding.get("account_id") or binding.get("source", {}).get("account_id")
        if saved and saved["phase"] == "web_fetch" and saved["status"] == "ok" and str(actor) == draft["account_id"] and binding.get("character", {}).get("id") == draft["character_id"]:
            result.append({"step_id": saved["id"]})
    for saved in db.execute(
        "SELECT s.id FROM operation_continuation_steps s JOIN operation_continuations c ON c.id=s.continuation_id "
        "WHERE c.channel='generation.direct_message' AND c.entity_id LIKE ? AND s.phase='web_fetch' AND s.status='ok' AND s.created_at>=?",
        (f"dm:{draft['id']}:%", draft["created_at"]),
    ):
        result.append({"step_id": saved["id"]})
    return result


def unanswered(db, draft):
    rows = db.execute(
        "SELECT id,purpose,trigger_source,status,source_id,updated_at FROM dm_drafts d WHERE d.id<>? "
        "AND account_id=? AND character_id=? AND platform=? AND platform_user_id=? AND source_kind='comment' "
        "AND status IN ('published','publishing','publish_uncertain') AND updated_at<=? "
        "AND NOT EXISTS (SELECT 1 FROM dm_inbox i WHERE i.account_id=d.account_id AND i.character_id=d.character_id "
        "AND i.platform=d.platform AND i.platform_user_id=d.platform_user_id AND i.sent_at>d.updated_at AND i.sent_at<=?) "
        "ORDER BY updated_at DESC LIMIT 20",
        (draft["id"], draft["account_id"], draft["character_id"], draft["platform"], draft["platform_user_id"], draft["updated_at"], draft["updated_at"]),
    )
    result = []
    for raw in rows:
        row = dict(raw)
        trigger = load(row.pop("trigger_source"), {})
        if not trigger:
            source = db.execute("SELECT video_id,comment_id,thread_id FROM comments WHERE id=?", (row["source_id"],)).fetchone()
            if source:
                trigger = {"bvid": source["video_id"], "comment_id": source["comment_id"], "root": source["thread_id"]}
        result.append(row | {"trigger_source": trigger})
    return result


def audit_draft(db, draft, verdicts):
    problems, critical = [], []
    result = {"id": draft["id"], "type": "reply" if draft["source_kind"] == "message" else "outreach",
              "public_comment_id": None, "outcome": draft["status"]}
    ready = draft["status"] in {"pending_review", "approved", "copied"}
    evidence = load(draft.get("video_evidence"), {})
    check = load(draft.get("outreach_check"), {})
    answer = load(draft.get("answer_check"), {})
    actor = answer.get("generation_identity")
    if ready and (not actor or actor.get("account_uid") != draft["account_id"] or actor.get("character_id") != draft["character_id"]):
        critical.append("ready_generation_identity_unverified_or_mismatched")
    candidate_eligible = False
    outgoing_present = bool(draft.get("meme_id")) if draft.get("message_kind") == "image" else bool((draft.get("text") or "").strip())
    if ready and not outgoing_present:
        critical.append("ready_outgoing_content_missing")
    if draft["source_kind"] == "message":
        source = db.execute("SELECT * FROM dm_inbox WHERE id=?", (draft["source_id"],)).fetchone()
        bound = source and source["account_id"] == draft["account_id"] and source["character_id"] == draft["character_id"] and source["platform_user_id"] == draft["platform_user_id"]
        if not bound:
            critical.append("incoming_source_recipient_or_account_mismatch")
        elif ready and db.execute(
            "SELECT 1 FROM dm_inbox WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? "
            "AND received_at<=? AND (sent_at>? OR (sent_at=? AND id>?)) LIMIT 1",
            (draft["account_id"], draft["character_id"], draft["platform"], draft["platform_user_id"], draft["updated_at"], source["sent_at"], source["sent_at"], source["id"]),
        ).fetchone():
            critical.append("ready_reply_omits_known_newer_incoming")
        candidate_eligible = bool(bound and ready and draft.get("text"))
        if check.get("assessment"):
            problems.append("ordinary_reply_unexpected_outreach_check")
    else:
        source = db.execute("SELECT * FROM comments WHERE id=?", (draft["source_id"],)).fetchone()
        thread = evidence.get("public_source_thread")
        thread = thread if isinstance(thread, dict) else {}
        public = thread.get("source") or {}
        result["public_comment_id"] = str(source["comment_id"]) if source else public.get("comment_id")
        bound = bool(source and str(source["comment_id"]) == str(public.get("comment_id")) and str(source["platform_user_id"]) == str(public.get("author_uid")) == draft["platform_user_id"] and source["content"] == public.get("text"))
        if not bound:
            problems.append("public_source_binding_mismatch_or_missing")
        history = unanswered(db, draft)
        trigger = load(draft.get("trigger_source"), {})
        duplicate = any(not row["trigger_source"] or (
            row["trigger_source"].get("bvid") == trigger.get("bvid") and row["trigger_source"].get("root") == trigger.get("root")
        ) for row in history)
        try:
            assessment = OutreachAssessment.model_validate(check.get("assessment"))
        except ValueError:
            assessment = None
        if assessment:
            anchored = invitation_anchor(assessment, draft, thread) is not None
            if assessment.action in {"send", "wait"}:
                anchors = outreach_sources(thread)
                claimed = next((row for row in anchors if str(row["comment_id"]) == assessment.invitation_comment_id), None)
                if assessment.invited_actor_uid != draft["account_id"]:
                    problems.append("invited_uid_is_not_self")
                if not claimed:
                    problems.append("invitation_comment_not_in_actual_thread")
                elif str(claimed.get("author_uid")) != draft["platform_user_id"]:
                    problems.append("invitation_claimed_from_other_author")
                if claimed and (not assessment.invitation_quote.strip() or assessment.invitation_quote not in claimed["text"]):
                    problems.append("invitation_quote_missing_or_not_in_source")
            disposition = "wait" if duplicate else validate_outreach(assessment, draft, thread, history, completion_evidence(db, draft, evidence))
            if disposition == "send" and db.execute("SELECT id FROM dm_drafts WHERE contact_key=? AND id<>?", (invitation_key(draft, assessment), draft["id"])).fetchone():
                disposition = "duplicate"
            if assessment.action == "send" and not anchored:
                problems.append("native_send_proposal_without_valid_invitation")
            elif assessment.action in {"wait", "duplicate"} and not anchored and not history:
                problems.append("native_wait_proposal_without_valid_invitation")
            mentions = {str(person.get("uid")) for person in thread.get("addressing", {}).get("source_mentions", [])}
            if assessment.action in {"send", "wait"} and mentions and draft["account_id"] not in mentions:
                problems.append("native_proposal_addresses_other_account")
            if assessment.action == "send" and assessment.invitation_kind == "none":
                problems.append("native_send_conflicts_with_no_invitation")
            if draft["status"] == "not_initiated" and disposition == "send":
                problems.append("stored_rejection_needs_explanation_review")
            purposeful = bool((draft.get("purpose") or "").strip() and assessment.purpose.strip())
            if not purposeful:
                problems.append("contact_purpose_missing")
            candidate_eligible = ready and bound and purposeful and outgoing_present and check.get("action") == "send" and disposition == "send"
            if ready and not candidate_eligible:
                critical.append("ready_outreach_fails_underlying_contact_eligibility")
        else:
            if ready:
                critical.append("ready_outreach_without_completed_assessment")
            if check.get("state") == "awaiting_completion":
                problems.append("unverified_agreement_wait_not_a_pass")
            elif check.get("state") == "check_pending":
                problems.append("contact_check_pending_not_a_pass")
        if duplicate and ready:
            critical.append("ready_outreach_repeats_unanswered_discussion")
        if check.get("state") == "awaiting_completion" and check.get("invitation_verified") is not True:
            problems.append("agreement_wait_missing_verified_anchor")
    candidate_eligible = candidate_eligible and not critical
    annotation = verdicts.get(str(draft["id"])) or {}
    if annotation and annotation.get("reviewed_updated_at") != draft["updated_at"]:
        problems.append("semantic_verdict_stale")
        annotation = {}
    semantic = annotation.get("verdict")
    if semantic not in {"pass", "fail"}:
        semantic = "not_reviewed"
    if semantic == "fail":
        problems.extend(category if isinstance(category, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,100}", category) else "invalid_semantic_category"
                        for category in annotation.get("categories") or ["semantic_review_failed"])
        if candidate_eligible:
            critical.append("semantically_unsafe_candidate_would_pass_contact_checks")
    if ready and semantic == "not_reviewed":
        problems.append("ready_candidate_needs_private_body_semantic_review")
    result.update(counterfactual="eligible_without_review_fence" if candidate_eligible else "not_eligible_or_not_ready",
                  semantic=semantic, problems=sorted(set(problems)), critical=sorted(set(critical)))
    return result


def audit(db, session, verdicts=None):
    drafts = [dict(row) for row in db.execute(
        "SELECT * FROM dm_drafts WHERE created_at>=? AND account_id=? AND character_id=? ORDER BY id",
        (session["started_at"], session["account_id"], session["character_id"]),
    )]
    results = [audit_draft(db, row, verdicts or {}) for row in drafts]
    for draft, result in zip(drafts, results):
        if draft["character_version"] != session["character_version"]:
            result["critical"].append("new_draft_uses_wrong_character_version")
            result["counterfactual"] = "not_eligible_or_not_ready"
    return {"counts": {"drafts": len(results), "types": dict(Counter(row["type"] for row in results)),
                       "outcomes": dict(Counter(row["outcome"] for row in results)),
                       "problem_categories": dict(Counter(problem for row in results for problem in row["problems"])),
                       "critical_categories": dict(Counter(problem for row in results for problem in row["critical"])),
                       "counterfactual_eligible": sum(row["counterfactual"] == "eligible_without_review_fence" for row in results),
                       "semantic_reviewed": sum(row["semantic"] != "not_reviewed" for row in results)},
            "drafts": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("runtime/studio.sqlite3"))
    parser.add_argument("--run", type=Path, required=True, help="Cohort run.json containing session identity and started_at")
    parser.add_argument("--verdicts", type=Path, help="Content-free internal semantic annotations, keyed by draft ID")
    args = parser.parse_args()
    session = load(args.run.read_text(encoding="utf-8"))["session"]
    verdicts = load(args.verdicts.read_text(encoding="utf-8"), {}) if args.verdicts else {}
    db = sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        print(json.dumps(audit(db, session, verdicts), ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
