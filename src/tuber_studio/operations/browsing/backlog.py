"""Read-only inventory and repeatable archival of the legacy automation backlog."""

import argparse
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from .comment_reader import PENDING_VISITS

VISITS = f"""
WITH events AS (
    SELECT id,account_id,created_at,timestamp,
           json_extract(payload,'$.character_id') character_id,
           COALESCE(json_extract(payload,'$.bvid'), 'dynamic:' || json_extract(payload,'$.oid')) resource,
           COALESCE(NULLIF(NULLIF(CAST(json_extract(payload,'$.root') AS TEXT),'0'),''),source_key,id) root
      FROM browsing_events WHERE kind='comment_visit' AND status IN ({PENDING_VISITS})
      AND json_extract(payload,'$.review_session_id') IS NULL
), observations AS (
    SELECT account_id,character_id,resource,root_id,MAX(created_at) latest
      FROM browsing_comment_timeline GROUP BY account_id,character_id,resource,root_id
), floors AS (
    SELECT e.id,MAX(MAX(e.timestamp,COALESCE(o.latest,0))) OVER floor latest,
           ROW_NUMBER() OVER (floor ORDER BY e.timestamp DESC,e.created_at DESC,e.id DESC) position
      FROM events e LEFT JOIN observations o ON e.account_id=o.account_id AND e.character_id=o.character_id
       AND e.resource=o.resource AND e.root=o.root_id
      WINDOW floor AS (PARTITION BY e.account_id,e.character_id,COALESCE(e.resource,e.id),e.root)
)
SELECT id,CASE WHEN latest<=0 THEN 'revisit_parked'
               WHEN latest<=:week THEN 'archived' WHEN latest<:recent THEN 'revisit_parked'
               WHEN position>1 THEN 'revisit_merged' ELSE 'retain' END AS outcome FROM floors
"""

REACTIONS = """SELECT i.id FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id
    WHERE i.kind IN ('comment_like','video_like','dynamic_like','comment_dislike','video_dislike')
      AND i.status IN ('pending_review','approved') AND i.created_at<=? AND r.preview=0
      AND json_extract(r.config,'$.review_session_id') IS NULL
      AND NOT EXISTS(SELECT 1 FROM account_writes w WHERE w.account_id=i.account_id AND w.intent=i.source_key)"""

PRIVATE = """SELECT i.id, i.platform, i.account_id, i.character_id, i.platform_user_id,
    (SELECT o.message_id FROM dm_operator_replies o WHERE o.platform=i.platform AND o.account_id=i.account_id
     AND o.character_id=i.character_id AND o.platform_user_id=i.platform_user_id
     AND i.sequence>0 AND o.sequence=i.sequence+1 ORDER BY o.sequence LIMIT 1) AS outgoing,
    EXISTS(SELECT 1 FROM dm_operator_replies o WHERE o.platform=i.platform AND o.account_id=i.account_id
     AND o.character_id=i.character_id AND o.platform_user_id=i.platform_user_id
     AND CASE WHEN i.sequence>0 AND o.sequence>0 THEN o.sequence>i.sequence ELSE o.sent_at>=i.sent_at END) AS ambiguous,
    EXISTS(SELECT 1 FROM dm_drafts d WHERE d.account_id=i.account_id AND d.character_id=i.character_id
     AND d.platform=i.platform AND d.platform_user_id=i.platform_user_id
     AND (d.status IN ('publishing','publish_uncertain') OR d.source_kind='message' AND d.source_id=i.id
      AND d.status IN ('rejected','copied','published'))) AS held
  FROM dm_inbox i WHERE i.account_id<>'' AND i.sent_at>0
  AND i.sent_at<COALESCE((SELECT CAST(value AS REAL) FROM kv WHERE key='message_inbox_cutoff:'||i.account_id||':'||i.character_id),0)
  AND NOT EXISTS(SELECT 1 FROM dm_incoming_settlements s WHERE s.incoming_id=i.id)"""


def install_flow_ledger(store):
    store.db.executescript(f"""
        CREATE INDEX IF NOT EXISTS browsing_revisit_scope ON browsing_events(account_id,
            json_extract(payload,'$.character_id'),json_extract(payload,'$.review_session_id'),
            CAST(json_extract(payload,'$.oid') AS TEXT),CAST(json_extract(payload,'$.root') AS TEXT))
        WHERE kind='comment_visit' AND status IN ({PENDING_VISITS});
        CREATE TABLE IF NOT EXISTS operation_flow_changes(
          account_id TEXT NOT NULL,character_id TEXT NOT NULL,kind TEXT NOT NULL,delta INTEGER NOT NULL,at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS operation_flow_scope ON operation_flow_changes(account_id,character_id,at);
        CREATE TRIGGER IF NOT EXISTS operation_flow_visit_insert AFTER INSERT ON browsing_events
        WHEN NEW.kind='comment_visit' AND NEW.status IN ({PENDING_VISITS})
              AND json_extract(NEW.payload,'$.review_session_id') IS NULL
        BEGIN
            INSERT INTO operation_flow_changes VALUES(NEW.account_id,COALESCE(json_extract(NEW.payload,'$.character_id'),''),'revisit',1,
                (julianday('now') - 2440587.5) * 86400.0);
        END;
        CREATE TRIGGER IF NOT EXISTS operation_flow_visit_settle AFTER UPDATE OF status ON browsing_events
        WHEN OLD.kind='comment_visit' AND OLD.status IN ({PENDING_VISITS}) AND NEW.status NOT IN ({PENDING_VISITS})
              AND NEW.status NOT IN ('revisit_parked','archived') AND json_extract(OLD.payload,'$.review_session_id') IS NULL
        BEGIN
            INSERT INTO operation_flow_changes VALUES(OLD.account_id,COALESCE(json_extract(OLD.payload,'$.character_id'),''),'revisit',-1,
                (julianday('now') - 2440587.5) * 86400.0);
        END;
        CREATE TRIGGER IF NOT EXISTS operation_flow_send_add AFTER INSERT ON operation_send_queue
        WHEN NEW.channel<>'browsing' OR EXISTS(SELECT 1 FROM browsing_intents i WHERE i.id=NEW.source_id
                                               AND i.kind IN ('reply_public','top_level'))
        BEGIN
            INSERT INTO operation_flow_changes VALUES(NEW.account_id,NEW.character_id,'send',1,
                (julianday('now') - 2440587.5) * 86400.0);
        END;
        CREATE TRIGGER IF NOT EXISTS operation_flow_send_settle AFTER DELETE ON operation_send_queue
        WHEN OLD.channel<>'browsing' OR EXISTS(SELECT 1 FROM browsing_intents i WHERE i.id=OLD.source_id
                                               AND i.kind IN ('reply_public','top_level'))
        BEGIN
            INSERT INTO operation_flow_changes VALUES(OLD.account_id,OLD.character_id,'send',-1,
                (julianday('now') - 2440587.5) * 86400.0);
        END;
    """)


def flow_window(store, round_row):
    if round_row["preview"] or json.loads(round_row["config"]).get("review_session_id"):
        return {}
    args = (round_row["account_id"], round_row["character_id"], round_row["created_at"])
    next_at = store.one("SELECT MIN(created_at) AS next_at FROM browsing_rounds WHERE account_id=? AND character_id=? "
                        "AND created_at>? AND preview=0 AND json_extract(config,'$.review_session_id') IS NULL", args)["next_at"]
    return {row["kind"]: {key: row[key] for key in ("added", "settled", "net")}
            for row in store.all("SELECT kind,COUNT(CASE WHEN delta>0 THEN 1 END) AS added,"
                                 "COUNT(CASE WHEN delta<0 THEN 1 END) AS settled,SUM(delta) AS net "
                                 "FROM operation_flow_changes WHERE account_id=? AND character_id=? AND at>=? "
                                 "AND at<? GROUP BY kind", (*args, next_at or time.time()))}


def inventory(db, *, now=None, apply=False):
    """Apply only local queue state from a fresh snapshot, with no transport."""
    now = time.time() if now is None else now
    with db:
        db.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        if apply:
            db.execute("CREATE TABLE IF NOT EXISTS dm_recovered_incoming(incoming_id INTEGER PRIMARY KEY)")
        visits = db.execute(VISITS, {"recent": now - 72 * 3600, "week": now - 7 * 86400}).fetchall()
        reactions = db.execute(REACTIONS, (now - 72 * 3600,)).fetchall()
        private = db.execute(PRIVATE).fetchall()
        approved = dict(db.execute("SELECT kind,COUNT(*) FROM browsing_intents WHERE status='approved' GROUP BY kind"))
        approved["imported"] = db.execute("SELECT COUNT(*) FROM drafts WHERE status='approved'").fetchone()[0]
        batches = db.execute("""SELECT COUNT(*),COUNT(DISTINCT r.account_id||'/'||r.character_id||'/'||i.bvid||'/'||i.kind)
            FROM browsing_batch_items i JOIN browsing_batches b ON b.id=i.batch_id
            JOIN browsing_rounds r ON r.id=b.round_id WHERE i.status IN ('later','daily_wait') AND r.preview=0""").fetchone()
        rejected = db.execute("SELECT COUNT(*) FROM operation_continuations WHERE state='checkpoint' AND channel IN ('browsing','comments')").fetchone()[0]
        unknown = sum(db.execute(f"SELECT COUNT(*) FROM {table} WHERE status='publish_uncertain'").fetchone()[0]
                      for table in ("drafts", "browsing_intents", "dm_drafts", "dynamic_posts"))
        messages = Counter()
        for incoming, _, _, _, _, outgoing, ambiguous, held in private:
            if outgoing:
                messages["settled"] += 1
                if apply:
                    db.execute("INSERT OR IGNORE INTO dm_incoming_settlements VALUES(?,?)", (incoming, outgoing))
            elif ambiguous or held:
                messages["check"] += 1
            else:
                messages["recover"] += 1
                if apply:
                    db.execute("INSERT OR IGNORE INTO dm_recovered_incoming VALUES(?)", (incoming,))
        if apply:
            db.executemany("UPDATE browsing_events SET status=? WHERE id=?",
                           [(outcome, identity) for identity, outcome in visits if outcome != "retain"])
            db.executemany("UPDATE browsing_intents SET status='archived',auto_authorized=0 WHERE id=?", reactions)
            db.executemany("DELETE FROM operation_send_queue WHERE channel='browsing' AND source_id=?", reactions)
        return {"visits": dict(Counter(outcome for _, outcome in visits)), "approved": approved,
                "expired_reactions": len(reactions), "private": dict(messages),
                "video_rows": batches[0], "video_work": batches[1], "rejected": rejected, "uncertain": unknown}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    uri = args.database.resolve().as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    with sqlite3.connect(uri, uri=True, timeout=20) as db:
        print(json.dumps(inventory(db, apply=args.apply), ensure_ascii=False))


if __name__ == "__main__":
    main()
