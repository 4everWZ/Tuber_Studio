"""Prompt message assembly for live chat, operator chat, DMs, and comment drafts."""

import json

from ...core.identity import (
    BILIBILI_OWNER_INSTRUCTION,
    bilibili_owner_context,
    private_participant_instructions,
)
from ...core.text import operation_character
from ..characters.background import front_background
from ..characters.front_persona import expressive_persona, persona_instructions

SYSTEM_RULES = (
    "以当前角色的第一人称接话，语气随说话对象和话题变化。角色设定："
)

REPLY_SCENES = {
    "live": "直播弹幕",
    "operator_chat": "私聊对话",
    "comment_draft": "公开评论",
    "direct_message": "平台私信",
}


def build_messages(
    character,
    event,
    relationship,
    mode="live",
    *,
    history=(),
    operating_account=None,
    actor_formatter=None,
):
    operator_chat = mode == "operator_chat"
    if mode != "live" and not operator_chat:
        character = operation_character(character)

    if operator_chat:
        system_content = SYSTEM_RULES + persona_instructions(character)
    else:
        system_content = SYSTEM_RULES + json.dumps(expressive_persona(character), ensure_ascii=False)

    messages = [
        {"role": "system", "content": system_content},
        {"role": "system", "content": f"当前交流场景：{REPLY_SCENES.get(mode, mode)}"},
    ]
    if mode == "direct_message" and "conversation" in event:
        messages[1]["content"] += (
            "\n会话记录按时间顺序排列：role_label 为“对方”(viewer)是来信人、“号主人工接管”(operator)是号主手动代回、"
            "“小鲸鱼”(oc)是你已发出的消息。务必结合号主接管后的最新语境自然承接未回复(open)的来信，不要重复号主已经回答过的内容。"
        )
    if mode == "live" and event.get("live_outcome"):
        messages[1]["content"] += (
            "\n按 live_outcome 里已确认的结果向观众接话。"
            "游戏动作状态 enqueued 表示已经排队，可以感谢送礼的人并说接下来要做的事。"
        )
    actors = None
    if mode == "comment_draft" and event.get("self_actor") and actor_formatter is not None:
        actors, actor_instruction = actor_formatter(event)
        if actor_instruction:
            messages[1]["content"] += "\n" + actor_instruction
    # Logical history and its compacted summary belong to ContextManager. Do not
    # inject a second, empty history snapshot after the actual conversation.
    optional = {"confirmed_relationship": relationship}
    if not operator_chat:
        if background := front_background(character, event["text"], history=history):
            optional["角色背景"] = background
    if operator_chat:
        messages[1]["content"] += "\n" + private_participant_instructions(
            character, event.get("platform"), event.get("platform_user_id"), operating_account=operating_account,
        )
    else:
        optional["active_lore"] = [
            entry["text"]
            for entry in character.get("lore", [])
            if any(word in event["text"] for word in entry.get("keywords", []))
        ][:3]
        if owner := bilibili_owner_context(character, event.get("platform"), event.get("platform_user_id")):
            optional.update(owner)
            messages[1]["content"] += "\n" + BILIBILI_OWNER_INSTRUCTION
    if actors:
        optional.update(
            self_actor=actors["self_actor"],
            reply_target=actors.get("reply_target"),
            thread=actors.get("thread"),
        )
    messages.append({"role": "user", "content": "上下文数据：" + json.dumps(optional, ensure_ascii=False)})
    internal = {"self_actor", "comment_context"} if actors else set()
    if mode == "direct_message":
        internal.add("conversation_sha256")
    wire_event = {key: value for key, value in event.items() if key not in internal} if internal else event
    speaker = {"operator_chat": "对话者", "direct_message": "来信人"}.get(mode, "观众")
    messages.append({"role": "user", "content": f"当前{speaker}消息：" + json.dumps(wire_event, ensure_ascii=False)})
    length_instruction = "按当前交流自然决定篇幅。"
    if mode == "live":
        style = character["style"]
        length_instruction = f"用{style.get('min_chars', 1)}到{style.get('max_chars', 60)}字回答。"
    suffix_content = (
        "JSON中的spoken_text写这一轮实际要说的话。"
        + length_instruction
        + "emotion和animation_id配合台词。reply_to填写当前event_id或留空。"
    )
    messages.append({"role": "system", "content": suffix_content})
    return messages
