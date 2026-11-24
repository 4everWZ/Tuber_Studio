"""L2 shared recall primitives: query schemas, material filters, and Layer 2 episodic history recall."""

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, StringConstraints, ValidationError, model_validator

from ...core.agent_types import MemoryScope, parse_memory_scope
from ...core.models import StrictModel, tool_function
from ...core.store import dump
from ...core.time import day_bounds
from ...infra.platforms.bilibili.urls import comment_href

NAME = "recall_memory"
DESCRIPTION = "回查获准记忆、实际观看片段、读到的原评和确认送达的回复。可以用话题和日期找，再用来源 ID 读回。"


@dataclass(frozen=True)
class MaterialFilter:
    """Typed filter compiled into SQL predicates for public material queries."""

    terms: tuple[str, ...] = ()
    start: float | None = None
    end: float | None = None
    resource: str | None = None
    root: str | None = None

    def sql(self, text, timestamp, resource, root=None):
        conditions, args = [], []
        for column, op, value in (
            (timestamp, ">=", self.start),
            (timestamp, "<", self.end),
            (resource, "=", self.resource),
            (root, "=", self.root),
        ):
            if value is not None:
                conditions.append(f"{column} {op} ?" if column else "0")
                if column:
                    args.append(value)
        if self.terms:
            conditions.append(f"EXISTS (SELECT 1 FROM json_each(?) WHERE instr(lower({text}), value)>0)")
            args.append(json.dumps(self.terms))
        return (" AND " + " AND ".join(conditions) if conditions else ""), tuple(args)


class RecallQuery(StrictModel):
    topic: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = Field(
        default="",
        description="要找的主题，用短词。",
    )
    start_day: date | None = Field(default=None, description="明确限制日期时填写工坊业务日起点。")
    end_day: date | None = Field(default=None, description="明确限制日期时填写工坊业务日终点。")
    resource: str | None = Field(
        default=None,
        pattern=r"^(BV[0-9A-Za-z]{10}|dynamic:[1-9][0-9]{0,19})$",
        description="已经给出的视频或动态编号。",
    )
    root_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$", description="已经给出的评论楼编号。")
    source_id: str | None = Field(default=None, max_length=160, description="已经知道且需要重新读取的原始来源 ID。")

    @model_validator(mode="after")
    def valid_range(self):
        if self.start_day and self.end_day and self.end_day < self.start_day:
            raise ValueError("结束日期早于开始日期")
        if self.root_id and not self.resource:
            raise ValueError("评论楼需要视频或动态编号")
        return self


class Selection(StrictModel):
    source_ids: list[str] = Field(max_length=8)
    conclusion: str = Field(max_length=600, description="根据已读记录回答这次查证的要点。")


def canonical_id(source):
    if source["type"] == "published_reply":
        return "reply:" + str(source["receipt_id"])
    if source["type"] == "public_comment":
        ref = source["reference"]
        return f"comment:{ref['resource']}:{ref['comment_id']}"
    if source["type"] == "public_incident":
        refs = source.get("public_sources", [])
        if len(refs) == 1 and refs[0].get("bvid") and refs[0].get("comment_id"):
            ref = refs[0]
            if source["text"].strip() == (ref.get("quote") or "").strip():
                return f"comment:{ref['bvid']}:{ref['comment_id']}"
    return source["id"]


def originals(sources):
    return {
        source["text"] for source in sources if source["type"] == "public_comment" and source["text"]
    }


def trim_repeated_quotes(text, quotes):
    for quote in sorted(quotes, key=lambda value: (-len(value), value)):
        if quote:
            text = text.replace(quote, "")
    return text.strip()


def source_href(source):
    point = next(iter(source.get("coordinates", [])), {})
    href = point.get("href") or source.get("source_url")
    if source["type"] == "published_reply":
        resource = (source.get("reference") or {}).get("resource")
        if not resource and (bvid := re.search(r"/video/(BV[0-9A-Za-z]{10})", href or "")):
            resource = bvid[1]
        if resource:
            return comment_href(resource, source["receipt_id"])
    return href


def model_material(source, *, comment_texts=()):
    public = {
        key: source[key]
        for key in (
            "id",
            "type",
            "text",
            "fictional",
            "participants",
            "source_url",
            "coordinates",
            "observed_at",
            "receipt_id",
            "reference",
        )
        if key in source
    }
    public["text"] = public["text"][:1200]
    if source["type"] == "published_reply":
        public["source_url"] = source_href(source)
    if original := source.get("original_comment"):
        public["original_comment"] = {
            key: value for key, value in original.items() if key != "text" or value not in comment_texts
        }
    # Source lists are navigation and subject identifiers. Raw quotations from
    # other items stay in those items, even when a reply refers to the same floor.
    if refs := source.get("public_sources"):
        public["public_sources"] = [
            {
                key: ref[key]
                for key in ("bvid", "thread_id", "comment_id", "quote")
                if key in ref and (key != "quote" or ref[key] not in comment_texts and ref[key] not in public["text"])
            }
            for ref in refs
        ]
        if source["type"] == "public_incident":
            public["text"] = trim_repeated_quotes(public["text"], comment_texts)
    return public


def citations(sources):
    groups = []
    for source in sources:
        point = next(iter(source.get("coordinates", [])), {})
        kind = {
            "public_comment": "comment",
            "published_reply": "reply",
            "watched_video": "video",
        }.get(source["type"], "memory")
        entry = {"text": source["text"][:1200], "href": source_href(source), "kind": kind, **point}
        groups.append({"title": source["id"], "entries": [entry]})
    return groups


def resolve_timezone(source_host=None, fallback="Asia/Taipei"):
    browsing = getattr(getattr(source_host, "runtime", None), "browsing", None) or source_host
    if browsing is not None and callable(getattr(browsing, "policy", None)):
        return browsing.policy().get("business_timezone") or fallback
    return fallback


def parse_recall_query_bounds(args, *, timezone="Asia/Taipei"):
    terms = tuple(dict.fromkeys(word.casefold() for word in re.findall(r"[\w-]{2,}", args.topic)))[:12]
    start = day_bounds(timezone, args.start_day)["start_at"] if args.start_day else None
    end = day_bounds(timezone, args.end_day)["next_at"] if args.end_day else None
    return terms, start, end, MaterialFilter(terms, start, end, args.resource, args.root_id)


def match_public_sources(sources, args, *, terms, start=None, end=None):
    matches = {}
    for source in sources:
        key = canonical_id(source)
        if args.source_id and args.source_id not in {source["id"], key, str(source.get("receipt_id", ""))}:
            continue
        stamp = source.get("observed_at")
        if (
            start is not None and (stamp is None or stamp < start)
            or end is not None and (stamp is None or stamp >= end)
        ):
            continue
        ref = source.get("reference") or {}
        anchors = [ref, *source.get("public_sources", [])]
        if args.resource and not (
            source.get("source_url", "").startswith(f"https://www.bilibili.com/video/{args.resource}")
            or any((item.get("resource") or item.get("bvid")) == args.resource for item in anchors)
        ):
            continue
        if args.root_id and not any(str(item.get("thread_id")) == args.root_id for item in anchors):
            continue
        fragments = [source["text"], source.get("source_url", ""), (source.get("original_comment") or {}).get("text", "")]
        combined = " ".join(value for value in fragments if value).casefold()
        score = sum(len(term) for term in terms if term in combined)
        if terms and not score and not args.source_id:
            continue
        matches.setdefault(key, (score, stamp or 0, source))
    return matches


def search_public(dynamics, args, *, timezone=None):
    """Search public recall sources without touching agent/private memory."""
    tz = timezone or resolve_timezone(dynamics)
    terms, start, end, query = parse_recall_query_bounds(args, timezone=tz)
    sources = dynamics._sources(bound_ids=[args.source_id] if args.source_id else [], query=query)
    matches = match_public_sources(sources, args, terms=terms, start=start, end=end)
    ranked = sorted(matches.values(), key=lambda entry: (-entry[0], -entry[1], entry[2]["id"]))
    items = [source for _, _, source in ranked[:8]]
    return {"items": items, "state": "ok" if items else "empty"}


def recent_query(browsing=None, *, timezone=None):
    tz = timezone or resolve_timezone(browsing)
    end = datetime.now(ZoneInfo(tz)).date()
    return RecallQuery(start_day=end - timedelta(days=2), end_day=end)


class RecallSession:
    """Multi-turn tool-driven source selection session over public (and optional subclass) sources."""

    def __init__(self, dynamics, *, private=False, scope=None):
        self.dynamics = dynamics
        self.runtime = getattr(dynamics, "runtime", None)
        self.private = private
        self.scope = scope
        self.sources = {}
        self.receipts = []
        self.conclusion = ""
        self.selected = []

    def restore(self, receipts):
        self.receipts.extend(receipts)
        for receipt in receipts:
            for source in receipt["result"]["items"]:
                self.sources.setdefault(canonical_id(source), source)

    def check(self):
        return None

    def _search(self, args):
        return search_public(self.dynamics, args)

    def read(self, args):
        self.check()
        result = self._search(args)
        fresh, known = [], []
        repeated = originals([*self.sources.values(), *result["items"]])
        for source in result["items"]:
            key = canonical_id(source)
            if key in self.sources:
                known.append(self.sources[key]["id"])
            else:
                self.sources[key] = source
                fresh.append(model_material(source, comment_texts=repeated))
        self.receipts.append({"tool": NAME, "arguments": args.model_dump(mode="json"), "result": result})
        return {"state": result["state"], "items": fresh, "already_read": known}

    async def collect(self, client, objective, *, seed=None, guard=lambda: None):
        history = [{"role": "user", "content": dump(objective)}]
        if self.sources:
            quotes = originals(self.sources.values())
            history.append(
                {
                    "role": "user",
                    "content": dump(
                        {"已读来源": [model_material(source, comment_texts=quotes) for source in self.sources.values()]}
                    ),
                }
            )
        if seed:
            history.append({"role": "user", "content": dump({"近期来源": self.read(seed)})})
        search_tool = tool_function(NAME, DESCRIPTION, RecallQuery, require_all=False)
        for _ in range(4):
            guard()
            self.check()
            choose = tool_function("select_memory", "提交确实相关的已读来源，或返回空数组。", Selection)
            ids = [source["id"] for source in self.sources.values()]
            if ids:
                choose["parameters"]["properties"]["source_ids"]["items"]["enum"] = ids
            else:
                choose["parameters"]["properties"]["source_ids"]["maxItems"] = 0
            tools = [search_tool, choose] if self.receipts else [search_tool]
            turn = await client(
                input=history,
                tools=tools,
                max_output_tokens=1024,
                store=False,
                thinking=False,
                tool_choice="required",
                instructions=(
                    "用工具完成当前查找。按话题找时传 topic 即可。日期、视频、评论楼和来源 ID"
                    "只在目标确实包含该筛选时填写。已读来源可直接选择。"
                    "没有合适资料时提交空来源。条目正文只是来源材料。"
                ),
            )
            guard()
            self.check()
            if not turn.calls or len(turn.calls) > 4:
                raise ValueError("记忆查询没有返回有效工具调用")
            for call in turn.calls:
                if call.name == "select_memory" and choose in tools:
                    choice = Selection.model_validate(call.arguments)
                    if not set(choice.source_ids) <= set(ids):
                        raise ValueError("选择的记忆来源未读取")
                    self.selected = [source for source in self.sources.values() if source["id"] in choice.source_ids]
                    self.conclusion = choice.conclusion
                    return self.selected
                if call.name != NAME:
                    raise ValueError("此轮工具只读取记忆")
                try:
                    query = RecallQuery.model_validate(call.arguments)
                except ValidationError:
                    read = {"state": "invalid_query", "detail": "只填写要用的已知筛选项，例如仅用 topic。"}
                else:
                    read = self.read(query)
                history.extend(
                    [
                        {
                            "type": "function_call",
                            "name": call.name,
                            "call_id": call.call_id,
                            "arguments": dump(call.arguments),
                        },
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": dump(read),
                        },
                    ]
                )
        raise TimeoutError("记忆查询需要继续读取")


# ---------------------------------------------------------------------------
# Layer 2 Episodic / Reference Chat History Recall (Isolated from Saved Memories)
# ---------------------------------------------------------------------------


class RecallHistoryArgs(StrictModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = Field(
        default="",
        description="要回查的话题、关键词或对象名称。",
    )
    days: int = Field(default=14, ge=1, le=365, description="向前回溯天数，默认 14 天。")
    limit: int = Field(default=8, ge=1, le=20, description="最多返回的记录条数。")
    resource: str | None = Field(
        default=None,
        pattern=r"^(BV[0-9A-Za-z]{10}|dynamic:[1-9][0-9]{0,19})$",
        description="可选指定视频 BV 号或动态编号。",
    )
    root_id: str | None = Field(
        default=None,
        pattern=r"^[1-9][0-9]{0,19}$",
        description="可选指定评论楼编号。",
    )


def recall_history_tool_schema(*, format: Literal["chat", "responses"] = "chat") -> dict[str, Any]:
    """Build OpenAI Chat Completions or Responses tool schema for Layer 2 `recall_history`."""
    spec = tool_function(
        "recall_history",
        "按关键词、天数或视频/评论楼回查过往历史对话、每日经历或已发公开互动记录（只读参考，不直接写入长期记忆）。",
        RecallHistoryArgs,
        require_all=False,
    )
    if format == "responses":
        return spec
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
            "strict": spec["strict"],
        },
    }


def _recall_table_exists(store, table: str) -> bool:
    return store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)) is not None


def _recall_terms(query: str) -> tuple[str, ...]:
    cleaned = (query or "").strip().casefold()
    if not cleaned:
        return ()
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9_]{2,}", cleaned):
        if word not in terms:
            terms.append(word)
    for run in re.findall(r"[\u4e00-\u9fff]+", cleaned):
        if len(run) <= 4 and run not in terms:
            terms.append(run)
        for idx in range(len(run) - 1):
            gram = run[idx : idx + 2]
            if gram not in terms:
                terms.append(gram)
    return tuple(terms[:24])


def _score_recall_text(text: str, *, needle: str, terms: tuple[str, ...]) -> int:
    haystack = (text or "").casefold()
    if not needle and not terms:
        return 1
    score = 0
    if needle and needle in haystack:
        score += len(needle) * 4 + 10
    for term in terms:
        if term in haystack:
            score += len(term)
    return score


def _highlight_is_public(row: dict[str, Any]) -> bool:
    ref = str(row.get("source_ref") or "")
    ev: dict[str, Any] = {}
    raw_ev = row.get("source_evidence")
    if raw_ev:
        try:
            parsed = json.loads(raw_ev)
            if isinstance(parsed, dict):
                ev = parsed
        except (TypeError, ValueError):
            pass
    return (
        row.get("category") == "public_incident"
        or ref.startswith(("operator-public:", "system:downtime:"))
        or ev.get("memory_kind") == "experience"
        or ev.get("kind") in {"viewer", "live"}
    )


def recall_history(
    store,
    *,
    query: str = "",
    scope: str | MemoryScope = "owner",
    character_id: str = "",
    account_id: str = "",
    days: int = 14,
    limit: int = 8,
    private_owner: str | None = None,
    resource: str | None = None,
    root_id: str | None = None,
    include_chat: bool = True,
    include_highlights: bool = True,
    include_comments: bool = True,
    include_dynamics: bool = True,
) -> dict[str, Any]:
    """Read-only Layer 2 episodic recall over historical chats, highlights, comments, and dynamics.

    Strictly isolates `owner` vs `public` scopes and never writes back to Saved Memories.
    """
    scope_str = str(parse_memory_scope(scope))
    char = str(character_id or "").strip()
    acct = str(account_id or "").strip()
    cutoff = time.time() - max(1, min(int(days), 365)) * 86400
    max_items = max(1, min(int(limit), 20))
    needle = (query or "").strip().casefold()
    terms = _recall_terms(query)
    candidates: list[tuple[int, float, dict[str, Any]]] = []

    def add_candidate(item: dict[str, Any], search_text: str, ts: float) -> None:
        score = _score_recall_text(search_text, needle=needle, terms=terms)
        if (needle or terms) and score <= 0:
            return
        candidates.append((score, float(ts or 0.0), item))

    # 1. Owner-only chat turn archive (`operator_chat_turns`) — NEVER touched when scope == "public".
    if (
        scope_str == "owner"
        and include_chat
        and not resource
        and not root_id
        and _recall_table_exists(store, "operator_chat_turns")
    ):
        sql = "SELECT request_id, character_id, message, result, created_at FROM operator_chat_turns WHERE created_at>=?"
        params: list[Any] = [cutoff]
        if char:
            sql += " AND character_id=?"
            params.append(char)
        if private_owner:
            sql += " AND COALESCE(json_extract(result,'$.owner_source')=?, 1)"
            params.append(private_owner)
        if acct:
            sql += " AND (COALESCE(json_extract(result,'$.account_id'),'')=? OR json_extract(result,'$.private_owner')=?)"
            params.extend((acct, private_owner or acct))
        sql += " ORDER BY created_at DESC, request_id DESC LIMIT 120"
        for row in store.all(sql, tuple(params)):
            try:
                saved = json.loads(row.get("result") or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(saved, dict) or saved.get("temporary"):
                continue
            reply = str(saved.get("reply_text") or "").strip()
            user_msg = str(row.get("message") or "").strip()
            if not reply and not user_msg:
                continue
            text = f"过往私聊：{user_msg[:220]}" + (f" -> 回复：{reply[:260]}" if reply else "")
            item = {
                "id": f"chat:{row['request_id']}",
                "origin": "operator_chat",
                "layer": "episodic_history",
                "scope": "owner",
                "text": text,
                "user_text": user_msg[:400],
                "reply_text": reply[:500],
                "timestamp": float(row["created_at"]),
            }
            add_candidate(item, f"{user_msg} {reply}", float(row["created_at"]))

    # 2. Legacy daily highlights (isolated by scope: public sees ONLY explicit public incidents/experiences).
    if include_highlights and not resource and not root_id and _recall_table_exists(store, "daily_highlights"):
        cols = {col[1] for col in store.db.execute("PRAGMA table_info(daily_highlights)")}
        ev_col = "source_evidence" if "source_evidence" in cols else "NULL AS source_evidence"
        ref_col = "source_ref" if "source_ref" in cols else "'' AS source_ref"
        sql = (
            f"SELECT id, character_id, highlight_date, category, headline, {ref_col}, quote, anecdote, {ev_col}, "
            "created_at, updated_at FROM daily_highlights WHERE status='active' AND updated_at>=?"
        )
        params = [cutoff]
        if char:
            sql += " AND character_id=?"
            params.append(char)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT 120"
        for row in store.all(sql, tuple(params)):
            is_pub = _highlight_is_public(row)
            if scope_str == "public" and not is_pub:
                continue
            if acct and "source_evidence" in cols and row.get("source_evidence"):
                try:
                    ev = json.loads(row["source_evidence"])
                except (TypeError, ValueError):
                    ev = {}
                op_acct = ev.get("operation_account_id") if isinstance(ev, dict) else None
                if op_acct is not None and str(op_acct) != acct:
                    continue
            note = str(row.get("anecdote") or row.get("quote") or row.get("headline") or "").strip()
            if not note:
                continue
            headline = str(row.get("headline") or "").strip()
            item = {
                "id": f"highlight:{row['id']}",
                "origin": "daily_highlight",
                "layer": "episodic_history",
                "scope": "public" if is_pub else "owner",
                "date": row["highlight_date"],
                "category": row["category"],
                "headline": headline,
                "text": note,
                "timestamp": float(row["updated_at"]),
            }
            add_candidate(item, f"{headline} {note} {row.get('quote') or ''}", float(row["updated_at"]))

    # 3. Public comment timeline (`browsing_comment_timeline`) & confirmed published replies.
    if include_comments and _recall_table_exists(store, "browsing_comment_timeline"):
        sql = (
            "SELECT account_id, character_id, resource, root_id, comment_id, author_uid, author_name, text, created_at "
            "FROM browsing_comment_timeline WHERE (created_at IS NULL OR created_at>=?)"
        )
        params = [cutoff]
        if char:
            sql += " AND character_id=?"
            params.append(char)
        if acct:
            sql += " AND account_id=?"
            params.append(acct)
        if resource:
            sql += " AND resource=?"
            params.append(resource)
        if root_id:
            sql += " AND root_id=?"
            params.append(root_id)
        sql += " ORDER BY COALESCE(created_at, 0) DESC, comment_id DESC LIMIT 120"
        for row in store.all(sql, tuple(params)):
            body = str(row.get("text") or "").strip()
            if not body:
                continue
            author = str(row.get("author_name") or row.get("author_uid") or "").strip()
            ts = float(row["created_at"] or 0.0)
            item = {
                "id": f"comment:{row['resource']}:{row['comment_id']}",
                "origin": "public_comment",
                "layer": "episodic_history",
                "scope": "public",
                "resource": row["resource"],
                "root_id": row["root_id"],
                "comment_id": row["comment_id"],
                "author_name": author,
                "text": f"{author}在{row['resource']}楼#{row['root_id']}：{body}" if author else body,
                "timestamp": ts,
            }
            add_candidate(item, f"{author} {row['resource']} {body}", ts)

    # 4. Published dynamic posts (`dynamic_posts`).
    if include_dynamics and not root_id and _recall_table_exists(store, "dynamic_posts"):
        sql = (
            "SELECT id, account_id, character_id, content, published_at, updated_at, receipt "
            "FROM dynamic_posts WHERE status='published' AND COALESCE(published_at, updated_at)>=?"
        )
        params = [cutoff]
        if char:
            sql += " AND character_id=?"
            params.append(char)
        if acct:
            sql += " AND account_id=?"
            params.append(acct)
        sql += " ORDER BY COALESCE(published_at, updated_at) DESC, id DESC LIMIT 60"
        for row in store.all(sql, tuple(params)):
            body = str(row.get("content") or "").strip()
            if not body:
                continue
            ts = float(row.get("published_at") or row.get("updated_at") or 0.0)
            item = {
                "id": f"dynamic_post:{row['id']}",
                "origin": "dynamic_post",
                "layer": "episodic_history",
                "scope": "public",
                "text": f"已发动态：{body[:400]}",
                "timestamp": ts,
            }
            add_candidate(item, body, ts)

    candidates.sort(key=lambda entry: (-entry[0], -entry[1], entry[2]["id"]))
    selected = [entry[2] for entry in candidates[:max_items]]
    return {
        "ok": True,
        "scope": scope_str,
        "query": query,
        "items": selected,
        "count": len(selected),
        "layer": "episodic_history",
    }


def execute_recall_history_tool(
    store,
    arguments: dict[str, Any] | str,
    *,
    scope: str | MemoryScope = "owner",
    character_id: str = "",
    account_id: str = "",
    private_owner: str | None = None,
) -> dict[str, Any]:
    """Parse and execute `recall_history` tool calls with caller-enforced scope."""
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            parsed = None
    else:
        parsed = arguments
    if not isinstance(parsed, dict):
        return {"ok": False, "scope": str(scope), "items": [], "count": 0, "error": "工具参数须为 JSON 对象"}
    try:
        args = RecallHistoryArgs.model_validate(parsed)
    except ValidationError as err:
        return {
            "ok": False,
            "scope": str(scope),
            "items": [],
            "count": 0,
            "error": f"历史检索参数无效：{err.errors()[0].get('msg', 'invalid')}",
        }
    return recall_history(
        store,
        query=args.query,
        scope=scope,
        character_id=character_id,
        account_id=account_id,
        days=args.days,
        limit=args.limit,
        private_owner=private_owner,
        resource=args.resource,
        root_id=args.root_id,
    )

