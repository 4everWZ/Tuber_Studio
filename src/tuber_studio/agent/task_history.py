"""Search immutable private task editions, then resolve their original rows under current read ownership."""

import re
from urllib.parse import urlsplit

from .owner_memory import history_preference

_ID = re.compile(r"(?<![\da-f])[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}(?![\da-f])", re.I)
_BV = re.compile(r"(?<![a-zA-Z0-9])BV[a-zA-Z0-9]{10}(?![a-zA-Z0-9])")
_URL = re.compile(r'<(https?://[^\s<>]+)>|(https?://[^\s<>"（）「」，。；！？]+)')
_VERSION = re.compile(r"(?<![a-z0-9])r(\d{1,9})\s*[/·.: -]?\s*v(\d{1,9})(?![a-z0-9])", re.I)


def _terms(text):
    terms = []
    for word in re.findall(r"[a-zA-Z0-9_]{3,}|[\u3400-\u9fff]{3,}", text):
        if word.isascii():
            terms.append(word)
        else:
            terms.extend(word[i:i + 3] for i in range(len(word) - 2))
    return list(dict.fromkeys(terms))[:64]


def _phrase(text):
    return '"' + text.replace('"', '""') + '"'


def _urls(text):
    return [wrapped or plain.rstrip(".,;)]") for wrapped, plain in _URL.findall(text)]


def _mentions(body, value):
    if value.startswith(("https://", "http://")):
        return value in _urls(body)
    return value in _BV.findall(body)


class TaskHistory:
    @staticmethod
    def has_anchor(query):
        return any(pattern.search(query) for pattern in (_ID, _BV, _URL, _VERSION))

    def __init__(self, runtime):
        self.runtime = runtime
        self.tasks = runtime.tasks
        self.store = runtime.store
        self.store.db.create_function("task_history_mentions", 2, _mentions, deterministic=True)
        self.store.db.create_function("edition_recallable", 2, self.tasks.edition_recallable)
        # External-content tables keep the authoritative text in the business rows.
        # Populate old editions and install maintenance triggers in the same transaction.
        with self.store.db as db:
            db.execute("BEGIN IMMEDIATE")
            for table, field in (("agent_tasks", "goal"), ("creative_editions", "body")):
                index = table + "_history_fts"
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (index,)).fetchone():
                    continue
                db.execute(f"CREATE VIRTUAL TABLE {index} USING fts5({field},content='{table}',tokenize='trigram')")
                insert = f"INSERT INTO {index}(rowid,{field}) VALUES(new.rowid,new.{field});"
                delete = f"INSERT INTO {index}({index},rowid,{field}) VALUES('delete',old.rowid,old.{field});"
                for suffix, event, mutation in (("ai", "INSERT", insert), ("ad", "DELETE", delete),
                                                ("au", f"UPDATE OF {field}", delete + insert)):
                    db.execute(f"CREATE TRIGGER {index}_{suffix} AFTER {event} ON {table} BEGIN {mutation} END")
                db.execute(f"INSERT INTO {index}({index}) VALUES('rebuild')")

    def candidates(self, query, goal_id=None):
        """Only return labels and version coordinates before the read-only routing decision."""
        if not history_preference(self.runtime)["history_enabled"]:
            return []
        scopes, params = self.tasks.visible_scopes()
        where = f"t.scope IN {scopes}"
        ids = list(dict.fromkeys(value.lower() for value in _ID.findall(query)))[:32]
        if not goal_id:
            if len(ids) == 1 and self.tasks.current(ids[0]) and self.tasks.workspace.goal(ids[0]):
                goal_id = ids.pop()
            else:
                projects = self.store.all(
                    "SELECT g.id FROM creative_goals g JOIN agent_tasks t ON g.id=t.id "
                    f"WHERE {where} AND instr(?,g.title)>0 LIMIT 2", (*params, query))
                if len(projects) == 1:
                    goal_id = projects[0]["id"]
        if goal_id:
            if not self.tasks.current(goal_id) or not self.tasks.workspace.goal(goal_id):
                return []
            where += " AND t.creative_goal_id=?"
            params = (*params, goal_id)
        if ids:
            where += " AND t.id IN (" + ",".join("?" for _ in ids) + ")"
            params = (*params, *(identity.lower() for identity in ids))
        urls = _urls(query)
        anchors = [*urls, *_BV.findall(_URL.sub(" ", query))]
        for url in urls:
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            bv = re.fullmatch(r"/video/(BV[a-zA-Z0-9]{10})/?", parsed.path)
            if bv and parsed.netloc == "www.bilibili.com" and not parsed.query and not parsed.fragment:
                anchors.append(bv[1])
        exact = list(dict.fromkeys(anchors))[:16]
        # A project title identifies the collection, not the most relevant run in it.
        title = self.tasks.workspace.goal(goal_id)["title"] if goal_id else ""
        terms = exact or ([] if ids else _terms(query.replace(title, "") if title else query))
        version = _VERSION.search(query)
        if not ids and not exact and not version:
            # Preserve the actual edition from the last private discussion as a candidate
            # even after a newer edition, account change, or role-card edit.
            owner, character = self.tasks.identity.reader()
            prior = self.store.one(
                "SELECT json_extract(result,'$.task_reference') AS id,json_extract(result,'$.task_artifact.task_revision') AS r,"
                "json_extract(result,'$.task_artifact.edition') AS v FROM operator_chat_turns WHERE character_id=? "
                "AND json_extract(result,'$.owner_source')=? AND json_extract(result,'$.task_artifact.edition') IS NOT NULL "
                "ORDER BY created_at DESC,request_id DESC LIMIT 1", (character, owner))
        else:
            prior = None
        columns = "t.id,t.goal,t.status,t.revision,e.task_revision,e.edition"
        version_filter = " AND e.task_revision=? AND e.edition=?" if version else ""
        edition_params = (*params, *(map(int, version.groups()) if version else ()))
        rows = []
        if terms:
            match = " OR ".join(map(_phrase, terms))

            def exact_filter(field):
                if not exact:
                    return ""
                return " AND (" + " OR ".join(f"task_history_mentions({field},?)" for _ in exact) + ")"

            rows = self.store.all(
                f"WITH hits AS (SELECT {columns},f.rank AS score,e.created,"
                "row_number() OVER (PARTITION BY t.id ORDER BY f.rank,e.created DESC,e.edition DESC) AS n "
                "FROM creative_editions_history_fts f JOIN creative_editions e ON e.rowid=f.rowid "
                f"JOIN agent_tasks t ON t.id=e.task_id WHERE {where}{version_filter} "
                "AND edition_recallable(t.id,e.task_revision) "
                f"AND creative_editions_history_fts MATCH ?{exact_filter('e.body')}) "
                "SELECT * FROM hits WHERE n=1 ORDER BY score,created DESC LIMIT 6", (*edition_params, match, *exact))
            if not version:
                rows += self.store.all(
                    f"SELECT t.id,t.goal,t.status,t.revision FROM agent_tasks_history_fts f "
                    f"JOIN agent_tasks t ON t.rowid=f.rowid WHERE {where} AND agent_tasks_history_fts MATCH ? "
                    f"{exact_filter('t.goal')} ORDER BY rank,t.updated DESC LIMIT 6", (*params, match, *exact))
        if not rows and prior:
            rows = self.store.all(
                f"SELECT {columns} FROM creative_editions e JOIN agent_tasks t ON t.id=e.task_id "
                f"WHERE {where} AND t.id=? AND e.task_revision=? AND e.edition=? AND edition_recallable(t.id,e.task_revision)",
                (*params, prior["id"], prior["r"], prior["v"]))
        if not rows and not exact:
            rows = self.store.all(
                f"WITH recent AS (SELECT {columns},e.created,t.updated,"
                "row_number() OVER (PARTITION BY t.id ORDER BY e.task_revision DESC,e.edition DESC) AS n "
                "FROM agent_tasks t LEFT JOIN creative_editions e ON t.id=e.task_id "
                f"WHERE {where}{version_filter} AND (e.edition IS NULL OR edition_recallable(t.id,e.task_revision)) "
                + ("" if ids or goal_id else "AND e.edition IS NOT NULL ") +
                ") SELECT * FROM recent WHERE n=1 ORDER BY created DESC,updated DESC LIMIT 6", edition_params)
        cards = {}
        for row in rows:
            key = row["id"]
            if key not in cards:
                cards[key] = {k: row[k] for k in ("id", "goal", "status", "revision")}
                edition = row if "edition" in row else self.store.one(
                    "SELECT task_revision,edition FROM creative_editions WHERE task_id=? "
                    "AND edition_recallable(task_id,task_revision) ORDER BY task_revision DESC,edition DESC LIMIT 1",
                    (key,))
                if edition and edition["edition"]:
                    cards[key]["artifact"] = {k: edition[k] for k in ("task_revision", "edition")}
                    if not exact and not version and edition["edition"] > 1:
                        original = self.store.one(
                            "SELECT edition,author FROM creative_editions WHERE task_id=? AND task_revision=? "
                            "ORDER BY edition LIMIT 1", (key, edition["task_revision"]))
                        if original and original["edition"] != edition["edition"]:
                            cards[key]["original_edition"] = {"task_revision": edition["task_revision"], **original}
                if len(cards) >= 6:
                    break
        return list(cards.values())

    def read(self, task_id, coordinate=None):
        task = self.tasks.current(task_id)
        if not task or not history_preference(self.runtime)["history_enabled"]:
            return None
        if coordinate:
            edition = self.store.one(
                "SELECT * FROM creative_editions WHERE task_id=? AND task_revision=? AND edition=?",
                (task_id, coordinate["task_revision"], coordinate["edition"]))
            if not edition:
                return None
        else:
            edition = self.tasks.workspace.latest(task_id)
        if edition and not self.tasks.edition_recallable(task_id, edition["task_revision"]):
            return None
        facts = {k: task[k] for k in ("goal", "status", "revision")}
        if edition:
            facts["delivery"] = {k: edition[k] for k in ("task_revision", "edition", "author", "body")}
        else:
            try:
                self.tasks._validate_reads(task["steps"], task["plan"])
            except ValueError:
                return None
            facts["message"] = self.tasks.view(task)["message"]
        return facts
