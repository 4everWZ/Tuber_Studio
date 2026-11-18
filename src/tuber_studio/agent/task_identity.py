"""Read ownership for the single authenticated Studio operator, separate from action grants."""

import json
from uuid import uuid4

from ..store import dump


class TaskIdentity:
    def __init__(self, store, character, binding):
        self.store = store
        self.character = character
        self.binding = binding

    def install(self):
        db = self.store.db
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS agent_task_authority(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), principal TEXT NOT NULL,
                    epoch INTEGER NOT NULL, binding TEXT)
            """)
            first_install = db.execute(
                "INSERT OR IGNORE INTO agent_task_authority VALUES(1,?,0,NULL)", (uuid4().hex,)
            ).rowcount
            self.principal = db.execute("SELECT principal FROM agent_task_authority").fetchone()[0]
            db.execute("""
                CREATE TABLE IF NOT EXISTS agent_task_scopes(
                    scope TEXT PRIMARY KEY, principal TEXT NOT NULL, character_id TEXT NOT NULL)
            """)
            db.execute("CREATE INDEX IF NOT EXISTS agent_task_read_scope ON agent_task_scopes(principal,character_id)")
            self._install_revocation(db)
            if first_install:
                for row in db.execute("SELECT DISTINCT scope FROM agent_tasks"):
                    self._map_legacy(db, row[0])

    def _map_legacy(self, db, raw):
        try:
            scope = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not (isinstance(scope, list) and len(scope) == 4 and isinstance(scope[0], str)
                and isinstance(scope[1], str) and type(scope[2]) is int
                and (scope[3] is None or isinstance(scope[3], str))):
            return
        db.execute("""
            INSERT INTO agent_task_scopes SELECT ?,?,character_id FROM character_versions
            WHERE character_id=? AND version=?
        """, (raw, self.principal, scope[1], scope[2]))

    @staticmethod
    def _install_revocation(db):
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS agent_task_binding_switch BEFORE INSERT ON kv
            WHEN (NEW.key='active_character' AND NEW.value IS NOT (SELECT value FROM kv WHERE key=NEW.key))
              OR (NEW.key='bilibili_account_last_profile' AND COALESCE(json_extract(NEW.value,'$.profile.uid'),'') IS NOT
                  COALESCE((SELECT json_extract(value,'$.profile.uid') FROM kv WHERE key=NEW.key),
                           (SELECT json_extract(binding,'$[0]') FROM agent_task_authority),''))
              OR (NEW.key='browsing_discovery_account' AND json_extract(NEW.value,'$.account_id') IS NOT
                   COALESCE((SELECT json_extract(value,'$.account_id') FROM kv WHERE key=NEW.key),
                            (SELECT json_extract(binding,'$[0]') FROM agent_task_authority)))
            BEGIN UPDATE agent_task_authority SET binding=NULL; END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS agent_task_card_edit AFTER UPDATE OF config ON character_versions
            WHEN OLD.config IS NOT NEW.config AND NEW.character_id=(
                SELECT json_extract(value,'$') FROM kv WHERE key='active_character')
                AND NEW.version=(SELECT active_version FROM characters WHERE id=NEW.character_id)
            BEGIN UPDATE agent_task_authority SET binding=NULL; END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS agent_task_version_switch BEFORE UPDATE OF active_version ON characters
            WHEN OLD.active_version<>NEW.active_version AND OLD.id=(
                SELECT json_extract(value,'$') FROM kv WHERE key='active_character')
            BEGIN UPDATE agent_task_authority SET binding=NULL; END
        """)

    def reader(self):
        return self.principal, self.character()

    def grant_current(self, scope):
        if len(scope) != 6 or scope[-2] != self.principal:
            return False
        return bool(self.store.one(
            "SELECT 1 AS current FROM agent_task_authority WHERE singleton=1 AND epoch=? AND binding=?",
            (scope[-1], dump(scope[:-2])),
        ))

    def __call__(self):
        binding = self.binding()
        raw = dump(binding)
        db = self.store.db
        state = db.execute("SELECT binding,epoch FROM agent_task_authority WHERE singleton=1").fetchone()
        if state["binding"] == raw:
            return [*binding, self.principal, state["epoch"]]
        with db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE agent_task_authority SET binding=?,epoch=epoch+1 WHERE binding IS NOT ?", (raw, raw))
            epoch = db.execute("SELECT epoch FROM agent_task_authority WHERE singleton=1").fetchone()[0]
            scope = [*binding, self.principal, epoch]
            db.execute("INSERT OR IGNORE INTO agent_task_scopes VALUES(?,?,?)", (dump(scope), self.principal, binding[1]))
        return scope
