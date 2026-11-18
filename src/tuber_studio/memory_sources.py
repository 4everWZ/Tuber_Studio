"""Original evidence and deletion identities for organized memories in the business DB."""

from contextlib import contextmanager

from .store import dump


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
        if not has_table(db, "memory_revisions"):
            _install_revisions(db)


def _install_revisions(db):
    db.execute("""CREATE TABLE memory_revisions(
        item_id INTEGER NOT NULL REFERENCES daily_highlights(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL,updated_at REAL NOT NULL,headline TEXT NOT NULL,note TEXT NOT NULL,
        source_key TEXT NOT NULL,status TEXT NOT NULL,owner_only INTEGER NOT NULL,
        PRIMARY KEY(item_id,revision))""")
    snapshot = """SELECT id,revision,updated_at,headline,anecdote,
        json_extract(source_evidence,'$.memory_source.key'),status,
        coalesce(json_extract(source_evidence,'$.owner_only'),0)
        FROM daily_highlights WHERE CASE WHEN json_valid(source_evidence)
        THEN json_extract(source_evidence,'$.memory_source.key') IS NOT NULL END"""
    db.execute("INSERT INTO memory_revisions " + snapshot)
    for action, name in [("INSERT", "insert"), ("UPDATE OF revision", "update")]:
        db.execute(f"CREATE TRIGGER memory_revision_{name} AFTER {action} ON daily_highlights "
                   "BEGIN INSERT INTO memory_revisions " + snapshot + " AND id=NEW.id; END")


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
                not isinstance(latest_scope[0], str) or latest_scope[1] != scope[1] or type(latest_scope[2]) != int):
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


def invalidate_turn(db, scope, key):
    if not key.startswith("turn:"):
        return
    account, character, version = scope
    mode = "operator:" + dump([account, character])
    if has_table(db, "grounded_memory"):
        db.execute("DELETE FROM grounded_memory WHERE scope=? AND character_id=? AND character_version=? AND event_id=?",
                   (mode, character, version, "operator:" + key[5:]))
    if has_table(db, "context_windows"):
        # A compacted window can depend on a turn without retaining its literal text.
        # The window's delete trigger also removes its archived histories.
        db.execute("DELETE FROM context_windows WHERE mode=? AND character_id=? AND character_version=?",
                   (mode, character, version))


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
