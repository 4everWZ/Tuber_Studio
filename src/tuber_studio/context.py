"""Persistent independent context windows for live and per-video operations."""

import json
import time

from .compaction import (
    ContextWindowExceeded,
    compact_history,
    insert_initial_context_before_last_real_user_or_summary,
    limits,
    needs_compaction,
    wire_messages,
)
from .providers import build_messages, public_error


class ContextManager:
    def __init__(self, settings, store, provider):
        self.settings, self.store, self.provider = settings, store, provider
        self.last_key = None
        store.execute("""CREATE TABLE IF NOT EXISTS context_windows(
            key TEXT PRIMARY KEY,mode TEXT,character_id TEXT,character_version INTEGER,
            history TEXT NOT NULL DEFAULT '[]',input_tokens INTEGER DEFAULT 0,
            carried_prefix_tokens INTEGER DEFAULT 0,compact_count INTEGER DEFAULT 0,
            last_error TEXT,updated_at REAL NOT NULL)""")

    def key(self, scope, character):
        return json.dumps([scope, character["id"], character["version"]], separators=(",", ":"))

    def window(self, scope, character):
        key = self.key(scope, character)
        self.store.execute(
            "INSERT OR IGNORE INTO context_windows(key,mode,character_id,character_version,updated_at) "
            "VALUES(?,?,?,?,?)",
            (key, scope, character["id"], character["version"], time.time()),
        )
        self.last_key = key
        return self.store.one("SELECT * FROM context_windows WHERE key=?", (key,))

    @staticmethod
    def parts(character, event, relationship, mode):
        scaffold = build_messages(character, event, relationship, "", [], mode)
        prefix = scaffold[:2]
        initial_context = [dict(scaffold[2], kind="context")]
        pending = dict(scaffold[3], kind="viewer", source_event_id=event["event_id"], source_complete=True)
        suffix = [scaffold[4]]
        return prefix, initial_context, pending, suffix

    @staticmethod
    def render(prefix, initial_context, history, pending=None, suffix=None):
        logical = history + ([pending] if pending else [])
        return wire_messages(
            prefix
            + insert_initial_context_before_last_real_user_or_summary(logical, initial_context)
            + (suffix or [])
        )

    async def prepare(self, scope, character, event, relationship=None, mode="live"):
        row = self.window(scope, character)
        prefix, initial, pending, suffix = self.parts(character, event, relationship or {}, mode)
        history = json.loads(row["history"])
        messages = self.render(prefix, initial, history, pending, suffix)
        count = await self.provider.count_tokens(messages)
        config = self.settings.data["llm"]
        total = count + config.get("max_tokens", 160)
        if needs_compaction(total, config, row["carried_prefix_tokens"]):
            if not history:
                raise ContextWindowExceeded("核心设定或当前输入已超窗，没有可压缩历史；请调整输入")
            await self._compact(row, prefix, initial)
            row = self.window(scope, character)
            history = json.loads(row["history"])
            messages = self.render(prefix, initial, history, pending, suffix)
            count = await self.provider.count_tokens(messages)
        if count + config.get("max_tokens", 160) > limits(config)[1]:
            raise ContextWindowExceeded("压缩后仍超出有效窗口；保留原始记录，不截断核心或当前输入")
        self.store.execute(
            "UPDATE context_windows SET input_tokens=?,updated_at=? WHERE key=?",
            (count, time.time(), row["key"]),
        )
        return messages

    async def _compact(self, row, prefix, initial):
        try:
            replacement = await compact_history(
                json.loads(row["history"]),
                prefix
                + [
                    {
                        "role": "system",
                        "content": "本次为内部上下文压缩，不是对观众发言。"
                        "角色台词口吻、字数和JSON输出约束仅适用于对外回复，不适用于本次摘要。"
                        "请保留已做事项、关键约束、未完成动作和必要引用；摘要不执行任何操作。",
                    }
                ],
                initial,
                self.provider,
                max_retries=self.settings.data["llm"].get("compaction_max_retries", 3),
            )
            count = await self.provider.count_tokens(self.render(prefix, initial, replacement))
            if (
                count + self.settings.data["llm"].get("max_tokens", 160)
                > limits(self.settings.data["llm"])[1]
            ):
                raise ContextWindowExceeded("压缩结果仍超窗，未替换原历史")
            # One atomic install only after a complete generation and rebuilt token count.
            self.store.execute(
                "UPDATE context_windows SET history=?,input_tokens=?,carried_prefix_tokens=?,"
                "compact_count=compact_count+1,last_error=NULL,updated_at=? WHERE key=?",
                (json.dumps(replacement, ensure_ascii=False), count, count, time.time(), row["key"]),
            )
        except BaseException as error:
            self.store.execute(
                "UPDATE context_windows SET last_error=?,updated_at=? WHERE key=?",
                (public_error(error), time.time(), row["key"]),
            )
            raise

    async def compact(self, scope, character, mode="live"):
        row = self.window(scope, character)
        if not json.loads(row["history"]):
            raise ValueError("当前范围没有可压缩历史；先完成互动或生成评论草稿")
        prefix, initial, _, _ = self.parts(character, {"text": "", "event_id": "compact"}, {}, mode)
        await self._compact(row, prefix, initial)
        return self.status()

    def record(self, scope, character, event, reply_text, mode="live"):
        row = self.window(scope, character)
        history = json.loads(row["history"])
        _, _, pending, _ = self.parts(character, event, {}, mode)
        # A delivered job or unique draft is recorded once. No in-flight speech is remembered as played.
        if any(
            item.get("source_event_id") == event["event_id"] and item.get("kind") == "viewer"
            for item in history
        ):
            return
        history.extend(
            [
                pending,
                {
                    "role": "assistant",
                    "content": reply_text,
                    "kind": "reply",
                    "source_event_id": event["event_id"],
                    "source_complete": True,
                },
            ]
        )
        self.store.execute(
            "UPDATE context_windows SET history=?,updated_at=? WHERE key=?",
            (json.dumps(history, ensure_ascii=False), time.time(), row["key"]),
        )

    def status(self):
        row = (
            self.store.one("SELECT * FROM context_windows WHERE key=?", (self.last_key,))
            if self.last_key
            else None
        )
        configured = self.settings.data["llm"]["context_tokens"]
        native = self.settings.data["llm"].get("native_context_tokens", 131072)
        server = self.provider.server_tokens
        return {
            "configured_tokens": configured,
            "server_tokens": server,
            "native_tokens": native,
            "verified": bool(server and server >= configured >= 131072 and native >= configured),
            "active_tokens": row["input_tokens"] if row else 0,
            "compact_count": row["compact_count"] if row else 0,
            "last_compaction_error": row["last_error"] if row else None,
            "profile_error": self.provider.profile_error,
            "scope": row["mode"] if row else None,
        }
