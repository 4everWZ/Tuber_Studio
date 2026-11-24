"""Map structured router tool calls and arrangement payloads to ChatIntent."""


import re

from fastapi import HTTPException

from ...memory_organizer import recall as recall_memory
from ..assistant_output import calendar_fields
from ..context import ChatContext
from ..lookup import outgoing_queue, workbench_tools, workshop_lookup
from ..lookup.operator_task_patch import TaskPatch, grounded_patch, patch_vocabulary, review_change
from ..lookup.personal_agent import Arrangement
from ..lookup.workshop_lookup import WorkshopLookup
from ..models import (
    _CURRENT_ACCOUNT,
    _LIVE_PROFILE,
    _MENTIONED_BV,
    _OWNER_FIELDS,
    _OWNER_PLATFORM,
    _OWNER_RELATION,
    _OWNER_UI_ONLY,
    _SETTING_ACTIONS,
    _WORKSHOP_OBJECTS,
    ChatIntent,
    DelegationBrief,
    IdentityRead,
    TurnRoute,
    memory_query,
)
from .disambiguation import _disambiguate_route
from .schemas import _as_str_list


def _parse_arrangement(raw) -> Arrangement | None:
    if not raw:
        return None
    if isinstance(raw, dict) and isinstance(raw.get("states"), str):
        raw = {**raw, "states": _as_str_list(raw["states"])}
    return Arrangement.model_validate(raw)


def _route_from_tool_call(call_name: str, args: dict) -> TurnRoute:
    if call_name == "chat":
        return TurnRoute(
            route="chat",
            action="chat",
            topic=args.get("topic", ""),
            reference=args.get("reference"),
            read_original=bool(args.get("read_original")),
            recall_reason=bool(args.get("recall_reason")),
            clock_fields=_as_str_list(args.get("clock_fields")),
        )
    if call_name == "instant_read":
        action = args["action"]
        lookup = None
        if args.get("lookup"):
            lookup = WorkshopLookup.model_validate(args["lookup"])
        elif action == workshop_lookup.LOOKUP_TOOL and args.get("collection"):
            day = args.get("day") or args.get("watch_day")
            if isinstance(day, str):
                day = {"今天": "today", "昨天": "yesterday"}.get(day.strip(), day.strip()) or None
                if day and day not in {"today", "yesterday"} and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    day = None
            terms = _as_str_list(args.get("terms"))
            lookup = WorkshopLookup(
                collection=args["collection"],
                day=day,
                terms=terms,
                part=args.get("part"),
                seconds=args.get("seconds"),
            )
        recall = None
        if args.get("recall"):
            recall = recall_memory.RecallQuery.model_validate(args["recall"])
        elif action == recall_memory.NAME and (args.get("topic") or args.get("terms")):
            topic_text = str(args.get("topic") or " ".join(_as_str_list(args.get("terms")))).strip()
            recall = recall_memory.RecallQuery(topic=topic_text)
        identities = [IdentityRead.model_validate(item) for item in args.get("identities", [])]
        if not identities and action == "identity_read" and args.get("identity_subject"):
            raw_subject = {"owner": "bilibili_owner"}.get(args["identity_subject"], args["identity_subject"])
            subjects = ("bilibili_owner", "operating_account") if raw_subject == "both" else (raw_subject,)
            identities = [IdentityRead(subject=subj, fields=["name", "uid", "homepage"]) for subj in subjects]
        return TurnRoute(
            route="instant_read",
            action=action,
            topic=args.get("topic", ""),
            object_ref=args.get("object_ref"),
            reference=args.get("reference"),
            position=args.get("position"),
            part=args.get("part"),
            seconds=args.get("seconds"),
            watch_day=args.get("watch_day") or args.get("day"),
            settings_group=args.get("settings_group"),
            setting_fields=_as_str_list(args.get("setting_fields")),
            settings_read_only=bool(args.get("settings_read_only", True)),
            publication_channel=args.get("publication_channel"),
            identities=identities,
            lookup=lookup,
            recall=recall,
            continue_research=bool(args.get("continue_research")),
        )
    if call_name == "direct_action":
        arrangement = _parse_arrangement(args.get("arrangement"))
        return TurnRoute(
            route="direct_action",
            action=args["action"],
            topic=args.get("topic", ""),
            memory_content=args.get("memory_content", ""),
            memory_tags=_as_str_list(args.get("memory_tags")),
            object_ref=args.get("object_ref"),
            reference=args.get("reference"),
            settings_group=args.get("settings_group"),
            game=args.get("game"),
            game_action=args.get("game_action"),
            source=args.get("source"),
            part=args.get("part"),
            seconds=args.get("seconds"),
            end_seconds=args.get("end_seconds"),
            arrangement=arrangement,
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "delegate_task":
        arrangement = _parse_arrangement(args.get("arrangement"))
        brief = DelegationBrief(
            goal=args["goal"],
            constraints=_as_str_list(args.get("constraints")),
            deliverable=args["deliverable"],
            material_refs=_as_str_list(args.get("material_refs")),
            read_tools=_as_str_list(args.get("read_tools")),
        )
        return TurnRoute(
            route="delegate_task",
            action="agent_task",
            topic=args.get("topic", ""),
            brief=brief,
            arrangement=arrangement,
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "steer_task":
        brief = None
        if args.get("brief"):
            brief = DelegationBrief.model_validate(args["brief"])
        elif args.get("goal") and args.get("deliverable"):
            brief = DelegationBrief(
                goal=args["goal"],
                constraints=_as_str_list(args.get("constraints")),
                deliverable=args["deliverable"],
                material_refs=_as_str_list(args.get("material_refs")),
                read_tools=_as_str_list(args.get("read_tools")),
            )
        effect = args.get("task_effect", "discuss")
        action = {
            "revise": "steer_task",
            "cancel": "cancel_task",
            "keep_current": "chat",
            "discuss": "chat",
        }[effect]
        return TurnRoute(
            route="steer_task",
            action=action,
            task_id=args.get("task_id"),
            reference=args.get("task_id"),
            task_effect=effect,
            change_quote=args.get("change_quote", ""),
            replacement_goal=args.get("replacement_goal", ""),
            brief=brief,
            read_original=bool(args.get("read_original")),
            recall_reason=bool(args.get("recall_reason")),
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "prepare_publication":
        return TurnRoute(
            route="prepare_publication",
            action="prepare_publication",
            publication_channel=args.get("publication_channel"),
            topic=args.get("topic", ""),
            object_ref=args.get("object_ref"),
            clarification_id=args.get("clarification_id") or None,
        )
    if call_name == "clarify":
        return TurnRoute(
            route="clarify",
            action="clarify",
            question=args["question"],
            topic=args["question"],
            clarification_id=args.get("clarification_id") or None,
        )
    raise ValueError(f"未知的路由工具：{call_name}")


def _workshop_decision(route: TurnRoute, context: dict, message: str = "") -> ChatIntent:
    args = route.lookup
    objects = context.get("objects", {})
    ref = route.object_ref or (route.reference if route.reference in objects else None)
    if args is None:
        if ref and (subject := objects.get(ref)):
            collection = "video" if subject.kind == "watch_history" else _WORKSHOP_OBJECTS.get(subject.kind, "video")
            args = WorkshopLookup(collection=collection, day=subject.identity if subject.kind == "watch_history" else None)
        elif len(mentioned := _MENTIONED_BV.findall(message)) == 1:
            args = WorkshopLookup(collection="video", identity=mentioned[0])
        else:
            return ChatIntent(action="clarify", topic="你想查哪条历史记录？")
    if args.collection == "video" and not args.identity and not ref:
        if len(mentioned := _MENTIONED_BV.findall(message)) == 1:
            args = args.model_copy(update={"identity": mentioned[0], "terms": []})
    decision = ChatIntent(action=workshop_lookup.LOOKUP_TOOL, position=route.position)
    if ref:
        subject = objects.get(ref)
        if subject and subject.kind == "watch_history" and args.collection in {"watch_count", "video"}:
            decision._lookup = args.model_copy(
                update={"day": args.day or WorkshopLookup(collection=args.collection, day=subject.identity).day}
            )
            return decision
        if subject is None or _WORKSHOP_OBJECTS.get(subject.kind) != args.collection:
            return ChatIntent(action="clarify", topic="你指的是哪条记录？说一下标题或编号就行。")
        decision._subject = subject
        args = args.model_copy(
            update={
                "identity": subject.identity,
                "terms": [],
                "part": args.part or route.part or (subject.part if (args.seconds is None and route.seconds is None) else None),
                "seconds": (
                    args.seconds
                    if args.seconds is not None
                    else (route.seconds if route.seconds is not None else (subject.seconds if args.part is None else None))
                ),
            }
        )
    decision._lookup = args
    return decision


async def route_to_chat_intent(context: ChatContext, route: TurnRoute, data, char, task_context, *,
                               client=None) -> ChatIntent:
    """Validate and lower a TurnRoute into a scoped ChatIntent for dispatcher/dialogue."""
    packet = task_context
    objects = packet.get("objects", {})
    candidates = [
        {
            "ref": ref,
            "index": idx,
            "kind": obj.kind,
            "label": obj.label,
            "identity": obj.identity,
        }
        for idx, (ref, obj) in enumerate(objects.items(), 1)
    ]
    route = _disambiguate_route(route, data.message, candidates)

    if route.route == "clarify" or route.action == "clarify":
        return ChatIntent(
            action="clarify",
            topic=route.question or route.topic or "你指的是哪一件事？",
            clarification_id=route.clarification_id,
        )

    if route.route == "chat" or (route.action == "chat" and route.route != "steer_task"):
        intent = ChatIntent(
            action="chat",
            topic=route.topic,
            reference=route.reference or route.task_id,
        )
        intent._clock_fields = tuple(calendar_fields(memory_query(data.message, char), route.clock_fields))
        intent._read_original = route.read_original
        intent._recall_reason = route.recall_reason
        return intent

    if route.route == "prepare_publication" or route.action == "prepare_publication":
        channel = route.publication_channel or outgoing_queue.named_channel(data.message)
        intent = ChatIntent(
            action="prepare_publication",
            publication_channel=channel,
            topic=route.topic,
            reference=route.reference,
            clarification_id=route.clarification_id,
        )
        if route.object_ref and route.object_ref in objects:
            intent._subject = objects[route.object_ref]
        return intent

    if route.route == "delegate_task" or route.action == "agent_task":
        arrangement = route.arrangement
        if (
            arrangement is not None
            and arrangement.kind == "once"
            and arrangement.local_date is None
            and arrangement.local_time is None
            and not re.search(r"预约|定时|明[天日]|后天|下周|周[一二三四五六日天]|星期|\d{1,2}\s*[:点时]", data.message)
        ):
            arrangement = None
        return ChatIntent(
            action="agent_task",
            topic=route.topic,
            brief=route.brief,
            arrangement=arrangement,
            clarification_id=route.clarification_id,
        )

    if route.route == "steer_task":
        cards = list({c["id"]: c for c in [*task_context.get("recent_task_cards", []), *task_context.get("history_task_cards", [])]}.values())
        target_id = route.task_id or route.reference
        if target_id is None and len(task_context.get("recent_task_cards", [])) == 1:
            target_id = task_context["recent_task_cards"][0]["id"]
        if route.task_effect in {"discuss", "keep_current"} or route.recall_reason:
            if target_id and not any(c["id"] == target_id for c in cards):
                return ChatIntent(action="clarify", topic="你指的是哪项交办？")
            if route.read_original and not any(c["id"] == target_id and c.get("original_edition") for c in cards):
                return ChatIntent(action="clarify", topic="能确认一下你指的是哪一版原稿吗？")
            if route.recall_reason and not target_id:
                return ChatIntent(action="clarify", topic="你想核对哪一份成果的理由？")
            intent = ChatIntent(action="chat", reference=target_id)
            intent._read_original = route.read_original
            intent._recall_reason = route.recall_reason
            return intent

        # Revise or cancel: preserve patch_vocabulary + grounded_patch + review_change guarantees
        card = next((c for c in task_context.get("recent_task_cards", []) if c["id"] == target_id), None)
        if card is None:
            return ChatIntent(action="clarify", topic="你想调整哪项交办？请说一下原任务的目标。")
        current_task = context.runtime.tasks.executable(card["id"])
        if not current_task or current_task["revision"] != card["revision"]:
            raise HTTPException(409, "任务或授权已变化，请重新查看任务卡。")
        clarification = task_context.get("clarification") or {}
        clarified = bool(route.clarification_id and str(route.clarification_id) == clarification.get("id"))
        evidence = (clarification["request"] + "\n" + data.message) if clarified else data.message
        patch_choices, bindings = patch_vocabulary([card], evidence)
        if not patch_choices and not clarification:
            return ChatIntent(action="chat", reference=None)
        requested_action = "cancel" if route.task_effect == "cancel" else "revise"
        if requested_action == "cancel" and "execution" not in card.get("editable", {}):
            return ChatIntent(action="clarify", topic="这次没有修改任务。请确认要调整哪项要求，以及要改成什么。")

        mentions_item = any(key.startswith("item:") for key in bindings)
        selected_patch = None
        if client is not None and mentions_item and patch_choices:
            action, selected_patch = await review_change(client, evidence, card, requested_action, patch_choices, bindings)
            if action == "new_work":
                return ChatIntent(action="agent_task", brief=route.brief, clarification_id=route.clarification_id)
            if action == "clarify":
                return ChatIntent(action="clarify", topic="请确认要改原交办的哪项要求，还是另做独立的一份。")
            if action == "discuss":
                return ChatIntent(action="chat", reference=target_id)
            if action == "recall_reason":
                intent = ChatIntent(action="chat", reference=target_id)
                intent._recall_reason = True
                return intent
            requested_action = action
        elif patch_choices and (quote := route.change_quote.strip()):
            allowed_fields = (lambda f: f in {"execution", "goal"}) if requested_action == "cancel" else (lambda f: f != "execution")
            for (field, subject), requirements in patch_choices.items():
                if not allowed_fields(field) or quote not in requirements:
                    continue
                candidate = TaskPatch(
                    field=field,
                    subject=subject,
                    requirement=quote,
                    replacement_goal=route.replacement_goal if field == "goal" else "",
                )
                if grounded_patch(candidate, card, evidence):
                    selected_patch = candidate
                    break
            if selected_patch is None:
                for (field, subject) in patch_choices:
                    if not allowed_fields(field):
                        continue
                    candidate = TaskPatch(
                        field=field,
                        subject=subject,
                        requirement=quote,
                        replacement_goal=route.replacement_goal if field == "goal" else "",
                    )
                    if grounded_patch(candidate, card, evidence):
                        selected_patch = candidate
                        break

        if (selected_patch is None or not grounded_patch(selected_patch, card, evidence)
                or (requested_action == "cancel" and (
                    selected_patch.field not in {"execution", "goal"} or "execution" not in card.get("editable", {})
                ))
                or (requested_action == "revise" and selected_patch.field == "execution")):
            return ChatIntent(action="clarify", topic="这次没有修改任务。请确认要调整哪项要求，以及要改成什么。")

        intent_action = "cancel_task" if requested_action == "cancel" else "steer_task"
        intent = ChatIntent(
            action=intent_action,
            reference=target_id,
            brief=route.brief,
            clarification_id=route.clarification_id,
        )
        intent._task_patch = selected_patch
        return intent

    # instant_read or direct_action
    action = route.action
    if action in _OWNER_UI_ONLY:
        raise HTTPException(422, "这项控制需要在设置界面操作")
    if (
        action == "owner_binding"
        or (
            action == "identity_read"
            and len(route.identities) == 1
            and route.identities[0].subject == "bilibili_owner"
        )
    ) and _LIVE_PROFILE.search(data.message):
        action = "owner_profile"
    if action == "identity_read" and not (
        _OWNER_RELATION.search(data.message)
        or _OWNER_PLATFORM.search(data.message)
        or _CURRENT_ACCOUNT.search(data.message)
        or (task_context.get("identity_reference") and _OWNER_FIELDS.search(data.message))
    ):
        return ChatIntent(action="clarify", topic="你想问哪一个账号或绑定关系？")
    if action == "watch_totals":
        decision = ChatIntent(action=workshop_lookup.LOOKUP_TOOL)
        decision._lookup = WorkshopLookup(collection="watch_count", day=route.watch_day or "today")
        decision._fixed_metrics = True
        return decision
    if action == workshop_lookup.LOOKUP_TOOL:
        return _workshop_decision(route, task_context, data.message)
    if action == recall_memory.NAME:
        decision = ChatIntent(action=recall_memory.NAME, topic=route.topic)
        decision._recall = route.recall or (recall_memory.RecallQuery(topic=route.topic) if route.topic else None)
        if route.object_ref and (subject := objects.get(route.object_ref)):
            if subject.kind == "recalled_source":
                base = decision._recall or recall_memory.RecallQuery(topic=route.topic)
                decision._recall = base.model_copy(update={"source_id": subject.identity})
        elif decision._recall and not decision._recall.source_id:
            recalled = [obj for obj in objects.values() if obj.kind == "recalled_source"]
            if len(recalled) == 1 and re.search(r"刚才|那[条个次]|来源|哪里读到|哪读的", data.message):
                decision._recall = decision._recall.model_copy(update={"source_id": recalled[0].identity})
        return decision

    subject = objects.get(route.object_ref) if route.object_ref else None
    decision = ChatIntent(
        action=action,
        topic=route.topic,
        memory_content=route.memory_content or None,
        memory_tags=route.memory_tags,
        reference=route.reference,
        clarification_id=route.clarification_id,
        position=route.position,
        part=route.part,
        seconds=route.seconds,
        end_seconds=route.end_seconds,
        source=route.source,
        game=route.game,
        game_action=route.game_action,
        settings_group=route.settings_group,
        publication_channel=route.publication_channel,
        arrangement=route.arrangement,
        brief=route.brief,
        identities=route.identities,
    )
    decision._subject = subject
    decision._continue_research = route.continue_research
    if subject and subject.kind in {"task", "artifact", "video", "event"} and not decision.reference:
        decision.reference = subject.identity
        if subject.part and decision.part is None:
            decision.part = subject.part
    if subject and subject.kind == "outgoing" and subject.identity in outgoing_queue.CHANNELS and not decision.publication_channel:
        decision.publication_channel = subject.identity
    if action in _SETTING_ACTIONS:
        if subject and subject.kind == "setting" and action == "daily_settings" and decision.settings_group is None:
            decision.settings_group = subject.identity
        if decision.settings_group is None and action == "daily_settings" and (prev := task_context.get("latest_tool")):
            decision.settings_group = prev.get("group")
        if action == "pet_settings" and decision.settings_group is not None:
            raise HTTPException(400, "宠物操作不能附带其他设置分组")
        group = "pets" if action == "pet_settings" else decision.settings_group
        read_only = bool(route.settings_read_only or route.route == "instant_read")
        if group:
            setting_hint = workbench_tools.requested_setting(data.message, char, task_context.get("latest_tool"))
            if setting_hint and setting_hint[1] == group:
                read_only = setting_hint[2]
                decision._setting_fields = setting_hint[3]
            elif read_only and action == "daily_settings":
                named = workbench_tools.named_setting_fields(group, data.message)
                decision._setting_fields = named or tuple(dict.fromkeys(route.setting_fields))
            if not subject and not workbench_tools.setting_target(group, data.message, character=char, read_only=read_only):
                if not setting_hint:
                    return ChatIntent(action="chat")
        decision._settings_read_only = read_only
    return decision
