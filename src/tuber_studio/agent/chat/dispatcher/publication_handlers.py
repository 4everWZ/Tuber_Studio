"""Outgoing queue and dynamic publication dispatch handlers."""

from ....operations.facade import create_operator_dynamic_draft, propose_dynamic_edit
from ..lookup import outgoing_queue
from ..lookup.dynamic_publication import PUBLICATION_TOOL, requested_draft_ids
from ..lookup.personal_agent import object_scope
from ..models import _DYNAMIC_ID, _DYNAMIC_URL


async def dispatch_publication_action(dispatcher, intent, char, request_id, data, kind, topic, request, *, persist_turn=None):
    ctx = dispatcher.ctx
    runtime = ctx.runtime
    if kind in {"outgoing_queue", "prepare_publication"}:
        scope = ctx.binding(char)
        if not scope[0]:
            if kind == "prepare_publication":
                return {
                    "reply_text": "还没有核验 B 站运营账号，这次没发。",
                    "card": {"type": "action_interaction", "data": {
                        "kind": "login", "href": "#accounts", "target": "B 站运营登录账号",
                    }},
                }
            return {"reply_text": "请先连接 B 站运营登录账号。", "card": None}
        channel = intent.publication_channel or outgoing_queue.named_channel(request)
        if channel is None and intent._subject and intent._subject.kind == "outgoing":
            channel = intent._subject.identity
        snapshot = outgoing_queue.queue_snapshot(ctx.store, scope, channel, prepare=kind == "prepare_publication")
        if kind == "prepare_publication" and channel == "dynamics":
            post_ids = outgoing_queue.pending_dynamic_ids(ctx.store, scope)
            if named := requested_draft_ids(request):
                post_ids = [key for key in post_ids if key in named]
            tasks_rt = runtime.tasks
            client = getattr(ctx.dynamics.account.adapter, "client", None)
            if (PUBLICATION_TOOL not in tasks_rt.tools or not callable(getattr(client, "post", None))
                    or getattr(client, "is_closed", False)):
                return {
                    "reply_text": "动态发布工具当前不可用，这次没发。可以先查看草稿。",
                    "assistant_read_scope": object_scope(runtime),
                    "publication_scope": scope,
                    "card": {"type": "publication_queue", "data": snapshot},
                }
            if not post_ids:
                return {
                    "reply_text": "当前没有可送审的待发动态，这次没发。",
                    "assistant_read_scope": object_scope(runtime),
                    "publication_scope": scope,
                    "card": {"type": "publication_queue", "data": snapshot},
                }
            result = {
                "reply_text": "已接下核对待发动态。核对后会在这里请你确认。",
                "card": {"type": "agent_task", "data": {"id": request_id}},
            }
            tasks_rt.create(
                request_id,
                request,
                source_key=request_id,
                prepared_step={"tool": PUBLICATION_TOOL, "arguments": {"post_ids": post_ids}},
                on_create=(lambda database: persist_turn(database, result)) if persist_turn else None,
            )
            return result
        return {
            "reply_text": outgoing_queue.queue_answer(snapshot),
            "publication_scope": scope,
            "card": {"type": "publication_queue", "data": snapshot},
            "tool_receipt": {"action": "read", "group": "outgoing", "values": {
                group["channel"]: group["counts"] for group in snapshot["groups"]
            }},
        }
    if kind == "dynamic":
        account = ctx.account()
        if intent.source == "discussion":
            history = ctx.assistant_history(char, account, discussion=True)
            if not history and not topic:
                return {"reply_text": "想写哪段讨论？", "card": None}
            material = history + ([{"topic": topic}] if topic else [])
            try:
                topic = await dispatcher.public_text(data.message, material, purpose="动态")
            except ValueError:
                return {"reply_text": "这段内容暂时提取不出可公开的主题，可以告诉我想分享哪一点。", "card": None}
            ctx.check_scope(char, account)
        row = await create_operator_dynamic_draft(ctx.dynamics, char, request_id, topic, account=account)
        return {"reply_text": "草稿已保存。", "card": {"type": "dynamic_draft", "data": ctx.draft_view(row)}}
    if kind == "edit_dynamic":
        identifiers = set(_DYNAMIC_URL.findall(data.message) or _DYNAMIC_ID.findall(data.message))
        if len(identifiers) != 1:
            return {"reply_text": "请给我一条要修改的动态链接或动态 ID。", "card": None}
        edit = await propose_dynamic_edit(ctx.dynamics, char, request_id, identifiers.pop(), data.message)
        return {"reply_text": "已准备好原动态的修订。", "card": {"type": "dynamic_edit", "data": {"request_id": edit["request_id"]}}}
    return None
