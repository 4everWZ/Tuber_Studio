"""Account/character scope, conversation history, object lifecycle and context projections (L2)."""

import asyncio
import base64
import json
import re

from fastapi import HTTPException
from fastapi.responses import Response

from ...core.identity import operating_account_facts
from ...core.models import content_hash
from ...core.store import dump
from ...live.facade import live_facade
from ...operations.facade import (
    browsing_policy,
    browsing_round,
    browsing_status,
    control_policy,
    full_video_status,
    get_dynamic_edit,
    get_dynamic_post,
    history_scope,
)
from ...persona.characters.front_persona import character_names
from ...persona.memory.daily import HIGHLIGHT_ACCOUNT_FILTER
from ..memory_organizer import recall as recall_memory
from ..memory_organizer.owner_memory import citation
from ..tasks.history import TaskHistory, history_preference
from .assistant_output import workbench_fields
from .delivery import delivery_turn_id, record_chat_delivery, waiting_delivery
from .lookup import operator_facts, outgoing_queue, workbench_tools, workshop_lookup
from .lookup.dynamic_publication import PUBLICATION_TOOL, publication_progress
from .lookup.operator_task_patch import editable_fields
from .lookup.personal_agent import (
    ConversationObject,
    binding_matches,
    object_scope,
    operating_account_object,
    owner_object,
    requests_background_work,
    requests_public_lookup,
)
from .models import (
    _CONTINUED_REFERENCE,
    _CURRENT_ACCOUNT,
    _DIRECT_RELATION,
    _EXPERIENCE_SOURCE,
    _LIVE_PROFILE,
    _ORDINAL_REFERENCE,
    _ORDINALS,
    _OWNER_FIELDS,
    _OWNER_PLATFORM,
    _OWNER_RELATION,
    ChatIntent,
    event_query_actions,
    memory_query,
)


class ChatContext:
    """Owns operator chat state, storage projections, scope verification and reference lifecycle."""

    def __init__(self, runtime, store, browsing, dynamics):
        self.runtime, self.store, self.browsing, self.dynamics = runtime, store, browsing, dynamics
        self.private_owner = runtime.tasks.identity.principal
        self.task_history = TaskHistory(runtime)
        runtime.daily_memory.private_owner = self.private_owner
        self.lock = asyncio.Lock()
        self.pending = {}
        store.execute(
            "CREATE TABLE IF NOT EXISTS operator_chat_turns ("
            "request_id TEXT PRIMARY KEY, character_id TEXT NOT NULL, character_version INTEGER NOT NULL, "
            "message TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        store.execute("CREATE TABLE IF NOT EXISTS operator_chat_cleared_requests (request_id TEXT PRIMARY KEY)")
        self.runtime.tasks.record_chat_delivery = record_chat_delivery
        store.execute("CREATE INDEX IF NOT EXISTS operator_chat_turn_age ON operator_chat_turns(created_at)")
        store.execute(
            "CREATE INDEX IF NOT EXISTS operator_chat_history "
            "ON operator_chat_turns(character_id,character_version,created_at DESC,request_id DESC)"
        )

    def scope(self, character):
        return "operator:" + dump([self.account(), character["id"]])

    def account(self):
        return str(history_scope(self.browsing)[0] or "")

    def binding(self, char):
        return [self.account(), char["id"], char["version"], char.get("_edit_identity")]

    def check_scope(self, char, account, *, account_may_resolve=False):
        if self.runtime.character != char or (account != self.account() and (account or not account_may_resolve)):
            raise HTTPException(409, "账号或角色已切换，请重新发送")

    def conversation(self, char):
        return self.store.get(f"operator_conversation:{self.private_owner}:{char['id']}", char["id"])

    def turn(self, request_id):
        return self.store.one("SELECT * FROM operator_chat_turns WHERE request_id=?", (request_id,))

    def current_turn(self, request_id, *, read_only=False):
        row = self.turn(request_id)
        char = self.runtime.character
        if row is None or row["character_id"] != char["id"]:
            raise HTTPException(404, "找不到当前角色的这段对话")
        saved = json.loads(row["result"])
        if saved.get("owner_source", self.private_owner) != self.private_owner:
            raise HTTPException(404, "找不到当前号主的这段对话")
        if read_only and saved.get("private_owner") == self.private_owner:
            return row
        if row["character_version"] != char["version"]:
            raise HTTPException(404, "找不到当前角色的这段对话")
        if not read_only and saved.get("account_id") != self.account():
            raise HTTPException(409, "请切换到当时的账号进行操作。")
        if not read_only and "execution_scope" in saved and saved["execution_scope"] != self.runtime.tasks.scope():
            raise HTTPException(409, "这张卡的执行授权已变化，请重新提出操作。")
        return row

    def references(self, char):
        events = {}
        tasks = {}
        focus = None
        latest_list = False
        account = self.account()
        rows = self.store.all(
            "SELECT request_id,result FROM operator_chat_turns WHERE character_id=? AND character_version=? "
            "AND json_extract(result,'$.account_id')=? ORDER BY created_at DESC,request_id DESC LIMIT 20",
            (char["id"], char["version"], account),
        )
        follow_up = True
        for age, row in enumerate(rows):
            result = json.loads(row["result"])
            card = result.get("card") or {}
            data = card.get("data", {})
            if card.get("type") == "operation_events" and not latest_list:
                latest_list = True
                for index, event in enumerate(data["items"], 1):
                    events[event["reference"]] = (index, event)
                if follow_up and focus is None and len(data["items"]) == 1:
                    focus = data["items"][0]["reference"]
            if card.get("type") == "operation_event":
                event = data["event"]
                if event.get("reference"):
                    events.setdefault(event["reference"], (0, event))
                    if follow_up and focus is None and not latest_list:
                        focus = event["reference"]
            elif card.get("type") != "operation_events":
                follow_up = False
            if card.get("type") == "video_browsing" and data.get("full_video"):
                task = browsing_round(self.browsing, data["id"])
                if task and (str(task["account_id"]), task["character_id"], task["character_version"]) == (
                    account, char["id"], char["version"]
                ):
                    selection = task["config"].get("operator_selection") or data.get("selection") or {}
                    if bvid := selection.get("bvid"):
                        tasks.setdefault(task["id"], {"status": task["status"], "bvid": bvid, "focused": age == 0})
        for row in self.store.all(
            "SELECT id,status,json_extract(config,'$.operator_selection.bvid') AS bvid FROM browsing_rounds "
            "WHERE account_id=? AND character_id=? AND character_version=? AND status IN ('waiting','queued','running') "
            "AND json_extract(config,'$.full_video')=1 ORDER BY updated_at DESC LIMIT 12",
            (account, char["id"], char["version"]),
        ):
            tasks.setdefault(row["id"], {"status": row["status"], "bvid": row["bvid"]})
        return events, tasks, focus

    def previous_card(self, char, account):
        row = self.store.one(
            "SELECT json_extract(result,'$.card.type') AS type FROM operator_chat_turns "
            "WHERE character_id=? AND character_version=? AND json_extract(result,'$.account_id')=? "
            "ORDER BY created_at DESC,request_id DESC LIMIT 1",
            (char["id"], char["version"], account),
        )
        return row["type"] if row else None

    def recall_visible(self, result):
        if "recalled_sources" not in result:
            return True
        if (result.get("recall_scope") != self.runtime.tasks.scope()
                or result.get("recall_history") != history_preference(self.runtime)):
            return False
        try:
            self.runtime.tasks._validate_reads(result.get("recall_audit", []), None)
            recall_memory.check_sources(self.dynamics, result["recalled_sources"])
        except (ValueError, PermissionError):
            return False
        return True

    def assistant_history(self, char, account, *, discussion=False, full_turns=False, recalled_sources=()):
        if not history_preference(self.runtime)["history_enabled"]:
            return []
        read_scope = object_scope(self.runtime)
        bindings = [char["id"], self.private_owner, read_scope, char["version"], account]
        portable = ""
        if not discussion:
            portable = " OR json_extract(result,'$.private_owner')=?"
            bindings.append(self.private_owner)
        rows = self.store.all(
            "SELECT request_id,message,result,character_version FROM operator_chat_turns WHERE character_id=? AND "
            "COALESCE(json_extract(result,'$.owner_source')=?,1) AND json_type(result,'$.call_origin') IS NULL AND "
            "(json_extract(result,'$.private')=1 OR json_extract(result,'$.assistant_read_scope')=?) AND "
            "((character_version=? AND json_extract(result,'$.account_id')=?)" + portable + ") "
            "ORDER BY created_at DESC,request_id DESC" + ("" if full_turns else " LIMIT 4"),
            bindings,
        )
        pairs, reply_prompts = [], {}
        for row in rows:
            source = "turn:" + row["request_id"]
            if source in recalled_sources:
                continue
            saved = json.loads(row["result"])
            if saved.get("call_origin") or not self.recall_visible(saved):
                continue
            if saved.get("clock_fields"):
                continue
            readback = saved.get("assistant_read_scope") == read_scope
            if (not readback and (not saved.get("private") or saved.get("memory_intent", "chat") != "chat")
                    or saved.get("organized_memory") or saved.get("memory") or saved.get("task_reference")):
                continue
            if not (discussion or readback or saved.get("assistant_context")):
                continue
            origin = (saved.get("account_id", account), char["id"], row["character_version"])
            if not self.runtime.daily_memory.history_turn_available(origin, source, self.private_owner):
                continue
            reply = saved.get("reply_text", "")
            if not reply:
                continue
            message = row["message"]
            reply_prompts.setdefault(saved["reply_text"], set()).add(message)
            if not full_turns:
                message, reply = message[:320], reply[:420]
            if row["character_version"] != char["version"] or saved.get("account_id") != account:
                message = "早先的私聊：" + message
            pairs.append((
                {"role": "user", "content": message},
                {"role": "assistant", "content": reply},
                saved["reply_text"],
            ))
        return [
            message
            for prompt, reply, original in reversed(pairs)
            if discussion or len(reply_prompts[original]) == 1
            for message in (prompt, reply)
        ]

    @staticmethod
    async def fit_assistant_history(provider, prefix, history, current, schema, output):
        async def count(start):
            candidate = [*prefix, *history[start * 2:], current]
            rendered = provider.prepare_json_messages(candidate, schema, max_tokens=output) if schema else candidate
            return await provider.count_tokens(rendered)

        start, end = 0, len(history) // 2
        if end:
            full = await count(0)
            limit = provider.capacity_window - min(output, 1024)
            if full > limit:
                limit = max(limit, await count(end))
                while start < end:
                    middle = (start + end) // 2
                    if await count(middle) <= limit:
                        end = middle
                    else:
                        start = middle + 1
        return [*prefix, *history[start * 2:], current]

    def task_context(self, char, account, data):
        semantic = not data.intent or data.intent.action == "chat"
        if semantic and not history_preference(self.runtime)["history_enabled"]:
            return {}
        rows = self.store.all(
            "SELECT json_extract(result,'$.card.data.id') AS id FROM operator_chat_turns "
            "WHERE character_id=? AND character_version=? AND json_extract(result,'$.account_id')=? "
            "AND json_extract(result,'$.card.type')='agent_task' "
            "AND COALESCE(json_extract(result,'$.conversation_id'),character_id)=? "
            "GROUP BY id ORDER BY MAX(created_at) DESC,id LIMIT 4",
            (char["id"], char["version"], account, self.conversation(char)),
        )
        cards = [
            {
                **{key: task[key] for key in ("id", "goal", "revision", "status")},
                **(editable_fields(self.runtime, task, self.task_history) if semantic else {}),
            }
            for row in rows
            if (task := self.runtime.tasks.executable(row["id"]))
            and (not data.creative_goal_id or task["creative_goal_id"] == str(data.creative_goal_id))
        ]
        context = {"recent_task_cards": cards} if cards else {}
        if semantic:
            hits = self.task_history.candidates(data.message, str(data.creative_goal_id) if data.creative_goal_id else None)
            if hits:
                context["history_task_cards"] = hits
            if data.opened_artifact:
                opened = data.opened_artifact
                identity = str(opened.task_id)
                coord = opened.model_dump(exclude={"task_id"})
                task = self.task_history.read(identity, coord)
                if not task or (
                    data.creative_goal_id
                    and (self.runtime.tasks.current(identity) or {}).get("creative_goal_id") != str(data.creative_goal_id)
                ):
                    raise HTTPException(409, "打开的成果已不可引用，请重新选择。")
                context["opened_artifact"] = {"id": identity, **coord}
                if not any(card["id"] == identity for card in [*cards, *hits]):
                    context.setdefault("history_task_cards", []).append({
                        "id": identity,
                        **{k: task[k] for k in ("goal", "status", "revision")},
                        "artifact": coord,
                    })
        last = self.store.one(
            "SELECT request_id,result FROM operator_chat_turns WHERE character_id=? AND character_version=? "
            "AND json_extract(result,'$.account_id')=? ORDER BY created_at DESC,request_id DESC LIMIT 1",
            (char["id"], char["version"], account),
        )
        if (last and (prior := json.loads(last["result"])).get("clarification_request")
                and prior.get("reply_text") != workbench_tools.OWNER_CONTROL_REPLY
                and prior.get("execution_scope") == self.runtime.tasks.scope()):
            context["clarification"] = {
                "id": last["request_id"],
                "request": prior["clarification_request"],
                "question": prior["reply_text"],
            }
        if last:
            prior = json.loads(last["result"])
            if ("object_scope" not in prior or prior["object_scope"] == object_scope(self.runtime)) and prior.get("direct_context"):
                context["latest_tool"] = prior["direct_context"]
            receipt = prior.get("tool_receipt") or {}
            if (prior.get("assistant_read_scope") == object_scope(self.runtime)
                    and prior.get("memory_intent") == "identity_read" and receipt.get("values")):
                context["identity_reference"] = {subject: list(fields) for subject, fields in receipt["values"].items()}
        if semantic:
            objects = self.conversation_objects(char, account)
            if objects:
                context["objects"] = objects
        return context

    def router_context_packet(self, char, account, data, context, *, history=None):
        """Build the bounded background routing context with recent turns and numbered candidates."""
        if data.temporary:
            recent = [{"role": item.role, "content": item.content[:400]} for item in data.window[-6:]]
        elif history_preference(self.runtime)["history_enabled"]:
            source = history if history is not None else self.assistant_history(char, account, discussion=False)
            recent = [{"role": item["role"], "content": item["content"][:400]} for item in source[-6:]]
        else:
            recent = []
        objects = context.get("objects", {})
        candidates = [
            {
                "ref": ref,
                "index": index,
                "kind": obj.kind,
                "label": obj.label,
                "identity": obj.identity,
                **({"part": obj.part} if obj.part is not None else {}),
                **({"seconds": obj.seconds} if obj.seconds is not None else {}),
            }
            for index, (ref, obj) in enumerate(objects.items(), 1)
        ]
        packet = {
            key: context[key]
            for key in (
                "recent_task_cards",
                "history_task_cards",
                "opened_artifact",
                "clarification",
                "latest_tool",
                "identity_reference",
                "video_selection",
            )
            if key in context
        }
        recent_ids = {card["id"] for card in packet.get("recent_task_cards", [])}
        if "history_task_cards" in packet and recent_ids:
            filtered_hits = [card for card in packet["history_task_cards"] if card["id"] not in recent_ids]
            if filtered_hits:
                packet["history_task_cards"] = filtered_hits
            else:
                packet.pop("history_task_cards", None)
        if candidates:
            packet["candidates"] = candidates
        if recent:
            packet["recent_dialogue"] = recent
        local = operator_facts.now(self.browsing)
        packet["local_calendar"] = workbench_fields("{{local_date}} {{local_weekday}} {{local_time}}", local)
        return packet

    def conversation_objects(self, char, account, turn_id=None):
        if not history_preference(self.runtime)["history_enabled"]:
            return {}
        scope = object_scope(self.runtime)
        rows = self.store.all(
            "SELECT result FROM operator_chat_turns WHERE character_id=? AND character_version=? "
            "AND json_extract(result,'$.account_id')=?"
            + (" AND request_id=?" if turn_id else " AND json_type(result,'$.task_delivery') IS NULL")
            + " ORDER BY created_at DESC,request_id DESC LIMIT 4",
            (char["id"], char["version"], account, *([str(turn_id)] if turn_id else [])),
        )
        objects = {}
        active_kinds = set()
        for row in rows:
            result = json.loads(row["result"])
            if (result.get("owner_source") != self.private_owner or result.get("object_scope") != scope
                    or result.get("conversation_id", char["id"]) != self.conversation(char)
                    or not result.get("conversation_objects") or not self.recall_visible(result)):
                break
            kinds = {raw["kind"] for raw in result["conversation_objects"]}
            if active_kinds and active_kinds.isdisjoint(kinds):
                break
            active_kinds.update(kinds)
            for raw in result["conversation_objects"]:
                subject = ConversationObject.model_validate(raw)
                if subject.kind == "owner" and not binding_matches(subject, char):
                    continue
                if subject.kind == "account" and subject != operating_account_object(
                    operating_account_facts(self.runtime.account.status())
                ):
                    continue
                if subject.kind in {"task", "artifact"} and not self.task_history.read(subject.identity):
                    continue
                objects.setdefault((subject.kind, subject.identity, subject.part), subject)
            if len(objects) >= 4:
                break
        return {f"obj{index}": obj for index, obj in enumerate(list(objects.values())[:4], 1)}

    def result_objects(self, result, intent, char):
        if "recalled_sources" in result:
            labels = {
                "private_memory": "查到的私有记忆",
                "watched_video": "观看片段",
                "public_comment": "读到的评论",
                "published_reply": "已送达的回复",
            }
            return [
                ConversationObject(
                    kind="recalled_source",
                    identity=item["id"],
                    label=labels.get(item["type"], "查到的记忆"),
                )
                for item in result["recalled_sources"][:4]
            ]
        if "lookup_items" in result:
            references = []
            for item in result["lookup_items"]:
                anchor = next(iter(item["entries"]), {})
                references.append(ConversationObject(
                    kind=item["kind"],
                    identity=item["identity"],
                    label=workshop_lookup.label(item)[:200],
                    revision=item["source_version"],
                    part=anchor.get("part"),
                    seconds=anchor.get("start"),
                ))
            return references
        if intent.action == "identity_read":
            return [ConversationObject.model_validate(item) for item in result.get("identity_objects", [])]
        card = result.get("card") or {}
        body = card.get("data") or {}
        card_type = card.get("type")
        if card_type == "video_activity" and body.get("available"):
            return [ConversationObject(
                kind="watch_history",
                identity=body["day"],
                label=f"{body['day']} 的观看记录，可查具体视频、片段和链接",
            )]
        if getattr(intent, "_identity_reply", False) or card_type == "owner_profile":
            owner = owner_object(char)
            return [owner] if owner else []
        if card_type == "publication_queue":
            return [
                ConversationObject(kind="outgoing", identity=group["channel"], label=group["label"] + "待发队列")
                for group in body["groups"]
            ]
        if receipt := result.get("tool_receipt"):
            group = receipt.get("group")
            if group == "web":
                return [
                    ConversationObject(
                        kind="web",
                        identity=source["href"],
                        label=group_item["title"][:200],
                        revision=source.get("published_at"),
                    )
                    for group_item in result.get("sources", [])
                    for source in group_item["entries"][:1]
                ]
            if group in {"pets", *workbench_tools.settings_menu()}:
                label = workbench_tools.settings_menu().get(group, "当前角色的宠物")
                return [ConversationObject(kind="setting", identity=group, label=label, revision=content_hash(receipt))]
        if task_id := result.get("task_reference") or (body.get("id") if card_type == "agent_task" else None):
            if task := self.runtime.tasks.current(task_id):
                artifact = result.get("task_artifact")
                revision = f"r{artifact['task_revision']} v{artifact['edition']}" if artifact else task["revision"]
                return [ConversationObject(
                    kind="artifact" if artifact else "task",
                    identity=task_id,
                    label=task["goal"][:200],
                    revision=revision,
                )]
        if card_type == "video_browsing" and body.get("id"):
            row = browsing_round(self.browsing, body["id"])
            if row:
                selection = body.get("selection") or {}
                ranges = selection.get("ranges") or []
                bvid = selection.get("bvid") or row.get("config", {}).get("video")
                if bvid:
                    parts = sorted({item["part"] for item in ranges}) or [None]
                    return [ConversationObject(kind="video", identity=body["id"], label=bvid, part=part) for part in parts[:4]]
        if card_type in {"operation_event", "operation_events"}:
            events = body.get("items") or [body["event"]]
            return [
                ConversationObject(
                    kind="event",
                    identity=event["reference"],
                    label=event["title"][:200],
                    revision=content_hash(event["source"]),
                )
                for event in events[:4]
                if event.get("reference")
            ]
        if intent.action == "operating_account":
            subject = operating_account_object(operating_account_facts(self.runtime.account.status()))
            return [subject] if subject else []
        if intent.action in {"operation_status", "live_status", "game_status"}:
            kind = {"operation_status": "operations", "live_status": "live", "game_status": "game"}[intent.action]
            label = {"operations": "自主运营状态", "live": "当前直播节目", "game": "当前游戏会话"}[kind]
            return [ConversationObject(kind=kind, identity=char["id"], label=label)]
        if memory := result.get("memory"):
            return [ConversationObject(
                kind="memory",
                identity=str(memory["id"]),
                label="引用的公开经历",
                revision=str(memory["updated_at"]),
            )]
        return []

    def source_followup(self, message, char, account):
        if re.search(r"不(?:用|要|必)|先别|如果", message) or not re.search(r"[？?]|什么|谁|吗|没", message):
            return None
        actions = event_query_actions(message)
        if len(actions) != 1 or "remember_event" in actions:
            return None
        ordinal = _ORDINAL_REFERENCE.search(message)
        if not ordinal and (not _CONTINUED_REFERENCE.search(message) or self.previous_card(char, account) != "operation_event"):
            return None
        return ChatIntent(action=actions.pop(), position=_ORDINALS[ordinal[1]] if ordinal else None)

    def bound_intent(self, data, char, context):
        if data.video_selection:
            return None
        message = data.message
        if (_DIRECT_RELATION.search(memory_query(message, char)) and not _OWNER_PLATFORM.search(message)
                and not workbench_tools.is_non_read_request(message)):
            return self.identity_reply(char)
        if requests_background_work(message) and not context.get("recent_task_cards") and not re.search(
            r"预约|定时|每[天日周]|明天|后天", message
        ):
            return ChatIntent(action="agent_task")
        objects = list(context.get("objects", {}).values())
        profiles = "|".join(re.escape(name) for name in character_names(char))
        owner_named = _OWNER_RELATION.search(message) or re.search(
            rf"(?:你的|(?:{profiles})的?)\s*(?:[Bb]\s*站|主页|空间)", message, re.I
        )
        owner_referenced = (
            len(objects) == 1
            and objects[0].kind == "owner"
            and (_OWNER_PLATFORM.search(message) or re.search(r"他|她|其|那|这个", message))
        )
        if (not _CURRENT_ACCOUNT.search(message) and _OWNER_FIELDS.search(message)
                and (owner_named or owner_referenced) and not workbench_tools.is_non_read_request(message)):
            action = "owner_profile" if _LIVE_PROFILE.search(message) else "owner_binding"
            return ChatIntent(action=action)
        if requests_public_lookup(message):
            return ChatIntent(action="web_lookup")
        if setting := workbench_tools.requested_setting(message, char, context.get("latest_tool")):
            action, group, read_only, fields = setting
            decision = ChatIntent(action=action, settings_group=group)
            decision._settings_read_only = read_only
            decision._setting_fields = fields
            return decision
        return None

    @staticmethod
    def identity_reply(char):
        owner = char.get("bilibili_owner")
        reply = f"我的主人是{owner['name']}呀。" if owner else "我还没有绑定 B 站号主呀。"
        decision = ChatIntent(action="chat")
        decision._identity_reply = True
        decision._reply_markdown = reply
        return decision

    def visible(self, result, *, historical=False):
        if not self.recall_visible(result):
            return {"reply_text": "这条回复引用的资料已变化。", "card": None}
        if "lookup_items" in result and result.get("assistant_read_scope") != object_scope(self.runtime):
            return {"reply_text": "这条记录当前不可查看。", "card": None}
        publication_scope = result.get("publication_scope")
        if publication_scope is not None and publication_scope != self.binding(self.runtime.character):
            return {"reply_text": "请切回当时的账号和角色查看稿件。", "card": None}
        if reference := result.get("task_reference"):
            artifact = result.get("task_artifact")
            if not self.task_history.read(reference, artifact):
                return {"reply_text": "当前无法引用这份任务记录。", "card": None}
            result = result | {"memory_dependent": True}
            if artifact:
                result["card"] = {"type": "history_artifact", "data": artifact}
        audit = result.get("organized_memory")
        try:
            self.runtime.validate_memory_delivery(audit, historical=historical)
        except ValueError:
            return {"reply_text": "这条回复引用的记忆已变更。", "card": None}
        if memory := result.get("memory"):
            rows = self.experience_rows(item_id=memory["id"])
            if (
                not rows or rows[0]["status"] not in {"active", "archived"}
                or rows[0]["updated_at"] != memory["updated_at"]
                or not self.runtime.daily_memory._source_evidence(rows[0])
            ):
                return {"reply_text": "这条公开经历当前不可引用。", "card": None}
        if memory or (audit and audit.get("organized_memory")):
            result = result | {"memory_dependent": True}
        if audit and audit.get("organized_memory"):
            scope = self.runtime.memory_organizer.scope()
            citations = []
            service = self.runtime.daily_memory
            for ref in audit["organized_memory"]:
                rows = service.organized(scope, item_id=ref["id"])
                if not rows or not (projected := service.owner_projection(rows[0])):
                    continue
                if key := ref.get("source_key"):
                    match = next((s for s in service.lookup_source(scope, "operator", key) if s["id"] == ref["id"]), None)
                    if not match:
                        continue
                    projected = projected | {"exact_source": match}
                citations.append(citation(projected))
            result = result | {"memory_citations": citations}
        result = {
            key: value
            for key, value in result.items()
            if key not in {
                "submission", "binding", "account_id", "memory", "confirmations", "organized_memory",
                "memory_intent", "assistant_context", "clarification_request", "private_owner",
                "owner_source", "execution_scope", "conversation_objects", "object_scope", "direct_context",
                "publication_scope", "call_origin", "task_origin", "lookup_items", "lookup_candidates",
                "lookup_request", "lookup_chosen", "recalled_sources", "recall_scope", "recall_history",
                "recall_audit",
            }
        }
        card = result.get("card")
        if card is None:
            return result
        if card["type"] == "agent_task":
            task = self.runtime.tasks.current(card["data"]["id"])
            if not task:
                return result
            data = self.runtime.tasks.view(task)
            event, _ = delivery_turn_id(task, task["status"], self.runtime.tasks._current_steps(task))
            if self.turn(event):
                data["delivered_in_chat"] = True
            return result | {"card": card | {"data": data}}
        if card["type"] == "dynamic_draft":
            identifier = card["data"]["id"]
            row = get_dynamic_post(self.dynamics, identifier) if self.store.one(
                "SELECT 1 FROM dynamic_posts WHERE id=?", (identifier,)
            ) else None
            return result | {"card": card | {"data": self.draft_view(row)}}
        if card["type"] == "publication_queue":
            data = outgoing_queue.queue_view(
                self.store, self.binding(self.runtime.character), card["data"], self.dynamics, self.draft_view,
            )
            return result | {"card": card | {"data": data}}
        if card["type"] == "dynamic_edit":
            try:
                edit = get_dynamic_edit(self.dynamics, 
                    card["data"]["request_id"], self.account(), self.runtime.character["id"],
                )
            except ValueError:
                return result | {"reply_text": "当前账号无法查看这条修订。", "card": None}
            return result | {"card": card | {"data": {
                "request_id": edit["request_id"],
                "original": json.loads(edit["original"])["text"],
                "caption": edit["outgoing"],
                "status": edit["status"],
                "error": edit["error"],
                "url": f"https://www.bilibili.com/opus/{edit['platform_id']}",
            }}}
        if card["type"] == "video_browsing":
            row = browsing_round(self.browsing, card["data"]["id"])
            if row:
                account, _, _ = history_scope(self.browsing)
                if not account or row["account_id"] != str(account):
                    return result | {"card": card | {"data": {"id": row["id"], "status": "unavailable"}}}
                if card["data"].get("full_video"):
                    status = full_video_status(self.browsing, row)
                    if focus := card["data"].get("focus"):
                        status["excerpt"] = self.video_excerpt(status, focus)
                    return result | {"card": card | {"data": status}}
                return result | {"card": card | {"data": card["data"] | {"status": row["status"]}}}
        if card["type"] == "live_studio_hud":
            data = self.live_view()
            if not historical and (program := card["data"].get("native_program")):
                data["native_program"] = program
            return result | {"card": card | {"data": data}}
        if card["type"] == "operation_control":
            return result | {"card": card | {"data": browsing_status(self.browsing) | card["data"]}}
        if card["type"] == "live_confirmation":
            return result | {"card": card | {"data": self.live_view() | card["data"]}}
        if card["type"] == "live_handoff":
            data = card["data"]
            return result | {"card": card | {"data": data | live_facade(self.runtime).handoff_result(data["session"], data["job_id"])}}
        if card["type"] == "experience_proposal":
            return result | {"card": {"type": "operation_event", "data": {"event": card["data"]["event"]}}}
        if card["type"] == "public_experiences":
            return result | {"card": card | {"data": {"items": self.saved_experiences()}}}
        if card["type"] == "memory_summary":
            memory = self.runtime.daily_memory
            scope = self.runtime.memory_organizer.scope()
            return result | {"card": card | {"data": {"groups": memory.summary(scope)}}}
        if card["type"] == "video_activity" and "available" not in card["data"]:
            return result | {"card": card | {"data": self.video_activity(card["data"])}}
        if card["type"] in {"operation_events", "operation_event"}:
            data = card["data"]
            items = data["items"] if card["type"] == "operation_events" else [data["event"]]
            if card["type"] == "operation_events" and "reply_sections" not in result:
                result["reply_sections"] = [self.event_excerpt(event) for event in items]
            return result | {"sources": [
                {
                    "title": event["title"],
                    "entries": operator_facts.event_sources(
                        self.browsing, event, include_saved_material=True,
                    ) + [entry for item in event.get("related", []) for entry in operator_facts.event_sources(self.browsing, item)],
                }
                for event in items
            ]}
        return result

    def visible_turn(self, row):
        result = json.loads(row["result"])
        if delivery := result.get("task_delivery"):
            turn = {
                "request_id": row["request_id"],
                "assistant_only": True,
                "message": "",
                "task_delivery": delivery,
                "character_name": result.get("character_name"),
            }
            task = self.runtime.tasks.current(delivery["task_id"])
            if (not task or result["account_id"] != self.account() or delivery["revision"] != task["revision"]
                    or (result.get("conversation_id") and result["conversation_id"] != self.conversation(self.runtime.character))
                    or delivery["status"] != task["status"]):
                return turn | {"hidden": True}
            steps = self.runtime.tasks._current_steps(task)
            if not steps and task["status"] == "cancelled":
                steps = task["steps"][-1:]
            if delivery["step_id"] and (not steps or steps[-1]["id"] != delivery["step_id"]):
                return turn | {"hidden": True}
            view = self.runtime.tasks.view(task)
            approval = steps[-1].get("approval") if view["status"] == "awaiting_approval" and steps else None
            if approval and not any(step.get("id") == delivery["step_id"] for step in view["steps"]):
                approval = None
            action = steps[-1] if steps and steps[-1]["tool"] == PUBLICATION_TOOL and self.runtime.tasks.can_control(task) else {}
            checkpoint = action.get("checkpoint") or {}
            items = (action.get("result") or {}).get("items")
            pending_items = checkpoint.get("items")
            if items is None and pending_items:
                items = list(pending_items.values())
            interaction = checkpoint.get("interaction")
            if delivery["status"] == "waiting" and (
                not waiting_delivery(steps) or delivery_turn_id(task, "waiting", steps)[0] != row["request_id"]
            ):
                return turn | {"hidden": True}
            reply = result.get("reply_text") or view["message"]
            if action and approval:
                reply = "核对这批待发动态后，勾选要公开发布的草稿。"
            elif items is not None and delivery["status"] != "waiting":
                reply = publication_progress(items, len(items))
            if items is not None:
                items = [{key: value for key, value in item.items() if key != "write_token"} for item in items]
            artifact = self.runtime.tasks.workspace.latest(task["id"]) if delivery["status"] == "completed" else None
            artifact_link = None
            if (artifact and artifact["task_revision"] == delivery["revision"]
                    and self.runtime.tasks.edition_recallable(task["id"], artifact["task_revision"])):
                artifact_link = (
                    f"/api/v1/agent/tasks/{task['id']}/artifacts/"
                    f"{artifact['task_revision']}/{artifact['edition']}?download=true"
                )
            sources = recall_memory.delivery_citations(view["steps"], (task["plan"] or {}).get("selected", {}))
            return turn | {
                "reply_text": reply,
                "reply_format": "markdown",
                **({"sources": sources} if sources else {}),
                "card": {"type": "task_delivery", "data": {
                    "id": task["id"],
                    "status": view["status"],
                    "approval": approval,
                    "revision": task["revision"],
                    "step_id": delivery["step_id"],
                    "can_control": self.runtime.tasks.can_control(task),
                    "items": items,
                    "interaction": interaction,
                    "artifact_download": artifact_link,
                    "title": task["goal"] if (task["creative_goal_id"] and task["creative_goal_id"] != task["id"]) else None,
                }},
            }
        card = result.get("card") or {}
        message = row["message"]
        if result.get("account_id") != self.account() and result.get("private_owner") != self.private_owner:
            message = ""
            result = {"reply_text": "请切换到当时的账号查看来源和任务。", "card": None}
            if card.get("type") in {"video_activity", "video_browsing"}:
                unavailable = {"available": False} if card["type"] == "video_activity" else {"status": "unavailable"}
                return {
                    "request_id": row["request_id"],
                    "message": message,
                    **result,
                    "card": {"type": card["type"], "data": unavailable},
                }
        return {
            "request_id": row["request_id"],
            "message": message,
            **self.visible(result, historical=True),
        }

    @staticmethod
    def event_excerpt(event):
        source = event["source"]
        quote = " ".join(source["text"].split())
        prefix = {"comment": "有条评论说", "subtitle": "字幕里提到", "visual": "画面里看到"}.get(source["kind"], "原记录写到")
        return f"{prefix}「{quote[:100]}{'…' if len(quote) > 100 else ''}」。"

    def event_summary(self, event, text):
        text = text.strip()
        names = "|".join(re.escape(name) for name in character_names(self.runtime.character))
        if not text or re.search(rf"https?://|<|已送达|(?:我|{names}).{{0,5}}(?:回了|回复了|发了|点赞了)", text):
            return self.event_excerpt(event)
        if event["source"]["kind"] == "comment" and not text.startswith(("有位观众", "有个观众", "有人", "评论区")):
            return self.event_excerpt(event)
        return text

    def live_view(self):
        status = live_facade(self.runtime).status()
        return {
            "state": status["state"],
            "muted": status["muted"],
            "busy": status["busy"],
            "enabled": self.runtime.settings.data.get("features", {}).get("live_enabled", False),
        }

    @staticmethod
    def draft_view(row):
        if row is None:
            return {"status": "unavailable"}
        return {
            key: row.get(key)
            for key in ("id", "content", "status", "receipt", "error", "account_id", "illustration", "meme_id", "comment_card", "review_key", "image_uploads")
        }

    def control_binding(self, char):
        control = control_policy(self.browsing)
        return {
            "scope": self.binding(char),
            "policy": browsing_policy(self.browsing),
            "messages": self.store.get("message_policy", {}),
            "dynamics": self.store.get("dynamic_policy", {}),
            "operation_revision": control["epoch"],
            "emergency": control["emergency"],
            "live_enabled": self.live_view()["enabled"],
        }

    def experience_rows(self, *, item_id=None, limit=50):
        char = self.runtime.character
        sql = (
            "SELECT * FROM daily_highlights WHERE character_id=? AND character_version=? AND "
            + _EXPERIENCE_SOURCE + " AND " + HIGHLIGHT_ACCOUNT_FILTER
        )
        account = self.account()
        params = [char["id"], char["version"], account, account]
        if item_id is not None:
            sql += " AND id=?"
            params.append(item_id)
        return self.store.all(sql + " ORDER BY highlight_date DESC,id DESC LIMIT ?", (*params, limit))

    def saved_experiences(self):
        memory = self.runtime.daily_memory
        scope = self.runtime.memory_organizer.scope()
        entries = [
            (entry, [ref["href"] for ref in row["source"].get("references", []) if ref.get("href")])
            for row in memory.organized(scope, limit=50)
            if row["memory_kind"] == "experience" and row["status"] == "active"
            and (entry := memory.owner_projection(row)) is not None
        ]
        for row in self.experience_rows():
            proof = memory._source_evidence(row)
            if not proof or row["status"] != "active":
                continue
            links = [ref["href"] for ref in proof.get("operation_sources", []) if ref.get("href")]
            if row["category"] == "public_incident":
                links.extend(
                    f"https://www.bilibili.com/video/{ref['bvid']}#reply{ref['comment_id']}"
                    for ref in json.loads(row["public_sources"] or "[]")
                )
            entries.append((row, links))
        entries.sort(key=lambda entry: (entry[0]["updated_at"], entry[0]["id"]), reverse=True)
        return [
            {"headline": row["headline"], "anecdote": row["anecdote"], "links": list(dict.fromkeys(links))}
            for row, links in entries[:50]
        ]

    def video_activity(self, period):
        account, character, _ = history_scope(self.browsing)
        base = {key: period[key] for key in ("day", "timezone", "start_at", "next_at")}
        if not account or account != period["account_id"]:
            return base | {"available": False}
        return workshop_lookup.viewing(self.store, account, character, base)

    @staticmethod
    def video_excerpt(status, focus):
        parts = (status.get("coverage") or {}).get("parts", [])
        if focus["part"] is None and len(parts) != 1:
            return {"error": "请指明要查看哪个分 P。"}
        selected = next((part for part in parts if focus["part"] in (None, part["part"])), None)
        if selected is None:
            return {"error": "该分 P 暂无已处理材料。"}
        seconds = focus["seconds"]
        ranges = [interval for interval in selected["ranges"] if interval["start"] <= seconds <= interval["end"]]
        highlights = [
            item
            for interval in ranges
            for item in interval.get("highlights", [])
            if abs(seconds - item["start"]) <= 15
        ]
        if not highlights:
            return {"error": "这个时点暂时没有可引用的材料。"}
        item = min(highlights, key=lambda row: abs(seconds - row["start"]))
        return item | {"part": selected["part"], "requested": seconds}

    def video_result(self, reference, text, *, focus=None):
        row = browsing_round(self.browsing, reference)
        data = {"id": reference, "full_video": True, "selection": row["config"]["operator_selection"]}
        if focus:
            data["focus"] = focus
        return {"reply_text": text, "card": {"type": "video_browsing", "data": data}}

    def inspection_image(self, request_id):
        row = self.current_turn(request_id)
        result = json.loads(row["result"])
        card = result.get("card") or {}
        if result.get("account_id") != self.account() or card.get("type") != "video_browsing":
            raise HTTPException(404, "画面不可用")
        task = self.store.one(
            "SELECT json_extract(payload,'$.inspection.preview') AS preview FROM browsing_tasks WHERE round_id=?",
            (card["data"]["id"],),
        )
        if not task or not task["preview"]:
            raise HTTPException(404, "画面不可用")
        preview = json.loads(task["preview"])
        return Response(base64.b64decode(preview["content"]), media_type=preview["mime"])
