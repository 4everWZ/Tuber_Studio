"""Original evidence and deletion identities for organized memories in the business DB."""

from contextlib import contextmanager

from ...core.store import dump


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def record_source(db, item_id, scope, key, quote, *, projection=None, thread=None):
    account, character, version = scope
    db.execute(
        "INSERT OR IGNORE INTO memory_sources(item_id,source_key,account,character,version,quote) "
        "VALUES(?,?,?,?,?,?)", (item_id, key, account, character, version, quote),
    )
    aliases = {key}
    if projection:
        for ref in projection["references"]:
            aliases.add(ref["href"])
            if ref.get("author_uid"):
                aliases.add(str(ref["author_uid"]))
            if "/video/" in ref["href"]:
                aliases.add(ref["href"].split("/video/", 1)[1].split("?", 1)[0].split("#", 1)[0].rstrip("/"))
        if thread:
            aliases.update([thread["resource"], f"thread:{thread['resource']}:{thread['root_id']}"])
    db.executemany(
        "INSERT OR IGNORE INTO memory_source_aliases(alias,item_id,source_key) VALUES(?,?,?)",
        [(alias, item_id, key) for alias in aliases],
    )


def original_turn(db, character, key):
    if not key.startswith("turn:") or not has_table(db, "operator_chat_turns"):
        return None
    return db.execute(
        "SELECT message,character_version,json_extract(result,'$.account_id') AS account,"
        "json_extract(result,'$.owner_source') AS owner FROM operator_chat_turns "
        "WHERE character_id=? AND request_id=?", (character, key[5:]),
    ).fetchone()


def edited_source_key(item_id, timestamp):
    return f"settings:edit:{item_id}:{timestamp}"


@contextmanager
def memory_transaction(db):
    """Commit standalone calls and preserve the caller's transaction when nested."""
    db.execute("SAVEPOINT memory_write")
    try:
        yield
    except BaseException:
        db.execute("ROLLBACK TO memory_write")
        raise
    finally:
        db.execute("RELEASE memory_write")


def migrate(service):
    db = service.store.db
    with memory_transaction(db):
        if not has_table(db, "memory_sources"):
            _install(service)
        # Leave pre-existing snapshots offline. Only the current row advances its stamp.
        db.execute("DROP TRIGGER IF EXISTS memory_revision_insert")
        db.execute("DROP TRIGGER IF EXISTS memory_revision_update")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='superseded_private_turns'").fetchone():
            # Older management edits retained the original source pointer. Index
            # the recorded edit as its own source without rewriting either record.
            edits = db.execute("""SELECT id,anecdote,character_id,character_version,
                json_extract(source_evidence,'$.operation_account_id'),json_extract(source_evidence,'$.owner_edit_at')
                FROM daily_highlights WHERE CASE WHEN json_valid(source_evidence) THEN
                    json_extract(source_evidence,'$.memory_kind')='private' AND
                    json_type(source_evidence,'$.operation_account_id')='text' AND
                    json_type(source_evidence,'$.owner_edit_at') IN ('integer','real') AND
                    json_extract(source_evidence,'$.owner_edit_at')>json_extract(source_evidence,'$.memory_source.at') END""")
            for item, note, character, version, account, at in edits:
                record_source(db, item, (account, character, version), edited_source_key(item, at), note)
            db.execute("""CREATE VIEW superseded_private_turns AS
                SELECT s.item_id,s.account,s.character,s.version,s.source_key FROM memory_sources s
                JOIN daily_highlights h ON h.id=s.item_id WHERE s.source_key LIKE 'turn:%'
                AND json_extract(h.source_evidence,'$.memory_kind')='private'
                AND (s.source_key != json_extract(h.source_evidence,'$.memory_source.key')
                     OR json_extract(h.source_evidence,'$.owner_edit_at') >
                        json_extract(h.source_evidence,'$.memory_source.at'))""")
            for row in db.execute("SELECT account,character,version,source_key FROM superseded_private_turns"):
                invalidate_turn(db, tuple(row[:3]), row[3])
            invalidate_outdated_reads(db)


def _install(service):
    db = service.store.db
    service._upgrade_pending()
    db.execute("ALTER TABLE daily_highlights ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    db.execute("ALTER TABLE daily_highlights ADD COLUMN owner_principal TEXT")
    db.execute("CREATE INDEX memory_private_identity ON daily_highlights(character_id,owner_principal,id)")
    db.execute("""CREATE TRIGGER memory_item_revision AFTER UPDATE OF
        headline,quote,anecdote,tags,source_evidence,status ON daily_highlights
        BEGIN UPDATE daily_highlights SET revision=OLD.revision+1 WHERE id=NEW.id; END""")
    db.execute("""CREATE TABLE memory_sources(
        item_id INTEGER NOT NULL REFERENCES daily_highlights(id) ON DELETE CASCADE,
        source_key TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
        version INTEGER NOT NULL, quote TEXT, PRIMARY KEY(item_id,source_key))""")
    db.execute("""CREATE TABLE memory_source_aliases(
        alias TEXT NOT NULL,item_id INTEGER NOT NULL,source_key TEXT NOT NULL,
        PRIMARY KEY(alias,item_id,source_key), FOREIGN KEY(item_id,source_key)
        REFERENCES memory_sources(item_id,source_key) ON DELETE CASCADE)""")
    db.execute("CREATE INDEX memory_source_alias_parent ON memory_source_aliases(item_id,source_key)")
    db.execute("CREATE INDEX memory_source_origin ON memory_sources(character,account,version,source_key)")
    db.execute("""CREATE TABLE memory_source_tombstones(
        owner TEXT NOT NULL,character TEXT NOT NULL,source_key TEXT NOT NULL,
        PRIMARY KEY(owner,character,source_key))""")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_organization (
        account TEXT, character TEXT, version INTEGER, source TEXT, revision TEXT, status TEXT,
        updated_at REAL, PRIMARY KEY(account,character,version,source))""")
    for row in service.store.all("SELECT * FROM daily_highlights WHERE source_evidence IS NOT NULL"):
        proof = service._source_evidence(row)
        if (not proof or not isinstance(proof.get("memory_source"), dict) or
                not isinstance(proof.get("operation_account_id"), str)):
            continue
        source = proof["memory_source"]
        if not isinstance(source.get("key"), str) or not source["key"]:
            continue
        owner = proof.get("private_owner") if proof.get("memory_kind") == "private" and not proof.get("scene") else None
        scope = (proof["operation_account_id"], row["character_id"], row["character_version"])
        latest_scope = source.get("scope") or scope
        if (not isinstance(latest_scope, (tuple, list)) or len(latest_scope) != 3 or
                not isinstance(latest_scope[0], str) or latest_scope[1] != scope[1] or type(latest_scope[2]) is not int):
            continue
        db.execute("UPDATE daily_highlights SET owner_principal=? WHERE id=?", (owner, row["id"]))
        projection = service._public_memory_projection(proof, source)
        record_source(db, row["id"], latest_scope, source["key"], row["quote"],
                      projection=projection, thread=source.get("thread"))
        for key in set(proof.get("preference_sources", [])) | {row["source_ref"]}:
            if not key or key == source["key"]:
                continue
            turn = original_turn(db, scope[1], key)
            if turn and ((owner and turn["owner"] == owner) or
                         (not owner and (turn["account"], turn["character_version"]) == (scope[0], scope[2]))):
                record_source(db, row["id"], (turn["account"], scope[1], turn["character_version"]), key, turn["message"])
            # An untraceable quote stays unavailable. Never substitute the latest quote.
    for row in db.execute("SELECT character,source FROM memory_organization WHERE status='deleted' AND source LIKE 'turn:%'"):
        turn = original_turn(db, row["character"], row["source"])
        if turn and turn["owner"]:
            db.execute("INSERT OR IGNORE INTO memory_source_tombstones VALUES(?,?,?)",
                       (turn["owner"], row["character"], row["source"]))
            invalidate_turn(db, (turn["account"], row["character"], turn["character_version"]), row["source"])


def invalidate_outdated_reads(db, item_id=None):
    if not has_table(db, "operator_chat_turns"):
        return
    # A reply may depend on a note without being the note's original turn. The
    # transcript is historical; invalidate only context derived from that turn.
    rows = db.execute("""
        SELECT DISTINCT json_extract(t.result,'$.account_id'),t.character_id,t.character_version,t.request_id
        FROM operator_chat_turns t JOIN json_each(t.result,'$.organized_memory.organized_memory') r
        JOIN daily_highlights h ON h.id=json_extract(r.value,'$.id') AND h.character_id=t.character_id
        WHERE json_extract(h.source_evidence,'$.memory_kind')='private' AND
          ((? IS NOT NULL AND h.id=?) OR (? IS NULL AND
            (h.updated_at!=json_extract(r.value,'$.updated_at') OR h.revision!=json_extract(r.value,'$.revision'))))
    """, (item_id, item_id, item_id))
    for account, character, version, request in rows:
        invalidate_turn(db, (account, character, version), "turn:" + request)


def invalidate_turn(db, scope, key):
    if not key.startswith("turn:"):
        return
    account, character, version = scope
    mode = "operator:" + dump([account, character])
    if has_table(db, "grounded_memory"):
        db.execute("DELETE FROM grounded_memory WHERE scope IN (?,?) AND character_id=? AND character_version=? AND event_id=?",
                   (mode, "operator:" + character, character, version, "operator:" + key[5:]))
    if has_table(db, "context_windows"):
        # A compacted window can depend on a turn without retaining its literal text.
        # The window's delete trigger also removes its archived histories.
        db.execute("DELETE FROM context_windows WHERE mode IN (?,?) AND character_id=? AND character_version=?",
                   (mode, "operator:" + character, character, version))


def install_chat_cleanup(db):
    """The chat and context components have both installed their tables at application startup."""
    if not all(has_table(db, name) for name in ("operator_chat_turns", "grounded_memory", "context_windows")):
        return
    # Match the JSON spelling used by OperatorChat._scope (including its separator).
    account_scope = ("'operator:[' || json_quote(json_extract(OLD.result,'$.account_id')) "
                     "|| ', ' || json_quote(OLD.character_id) || ']'")
    db.execute(f"""CREATE TRIGGER IF NOT EXISTS forget_deleted_chat_context AFTER DELETE ON operator_chat_turns
        BEGIN
          DELETE FROM grounded_memory WHERE scope IN ({account_scope}, 'operator:' || OLD.character_id)
            AND character_id=OLD.character_id AND character_version=OLD.character_version
            AND event_id='operator:' || OLD.request_id;
          DELETE FROM context_windows WHERE mode IN ({account_scope}, 'operator:' || OLD.character_id)
            AND character_id=OLD.character_id AND character_version=OLD.character_version;
        END""")
