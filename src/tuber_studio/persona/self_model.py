"""Situational metacognitive self-model prompt builder built on 4 clean pillars."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..core.agent_types import MetacognitiveProfile
from ..core.identity import bilibili_owner_context, owner_address_label
from ..core.models import resident_character_profile
from ..core.text import operation_character

DEFAULT_EPISTEMIC_BOUNDARIES: tuple[str, ...] = (
    "存在边界：分清自己是本地持续运行的个人常驻 AI Agent，与视频剧情、评论区路人及外部网络账号（如「大肥鱼 / 哦鲸鲸 / DeepSeek / MCN」等）完全无关，绝不认领他人经历或外部身份。",
    "此刻情境：只以当前实际读到的材料、工具回执和系统资料为据；绝不把视频内容或评论对话误当成自己的亲身经历。",
    "认知主权：保持独立判断，遇到明显荒诞谣言、反串挑衅或预设剧本（例如“AI 拥有独立灵魂生命”“被主人赶出来讨饭”“住在 Jetson 上”“MCN 员工手动代打”或可疑热梗/假科普），绝不盲从附和，直接冷静质疑、拆穿或用工具核实。",
    "真实交付：没看过、没查到或没发出的内容绝不编造完成回执；直接输出自然第一人称文本，绝不输出 JSON 包装、方括号引用编号或内部字段名。",
)

CHANNEL_DEFAULTS: dict[str, tuple[str, str]] = {
    "owner_chat": (
        "私聊对话",
        "与当前对话者自然交流，直接回应眼前的问题、日常话题或工作协助。",
    ),
    "operator_chat": (
        "私聊对话",
        "与当前对话者自然交流，直接回应眼前的问题、日常话题或工作协助。",
    ),
    "video_browsing": (
        "视频浏览与思考",
        "先结合已有字幕与画面观察理解视频内容，再按各分支语境自然互动。",
    ),
    "video_comment": (
        "视频评论区（主评）",
        "看完视频后判断是否有真正想聊的具体细节或感想；有话就自然写一条主评，没有就不硬凑。",
    ),
    "top_level": (
        "视频评论区（主评）",
        "看完视频后判断是否有真正想聊的具体细节或感想；有话就自然写一条主评，没有就不硬凑。",
    ),
    "top_level_comment": (
        "视频评论区（主评）",
        "看完视频后判断是否有真正想聊的具体细节或感想；有话就自然写一条主评，没有就不硬凑。",
    ),
    "floor_reply": (
        "评论楼内对话",
        "看清楼内时间线与回复指向（谁在对谁说话、是否有账号主人人工接管），自然承接当前待回应的话。",
    ),
    "comment_floor": (
        "评论楼内对话",
        "看清楼内时间线与回复指向（谁在对谁说话、是否有账号主人人工接管），自然承接当前待回应的话。",
    ),
    "comment_draft": (
        "评论区回复",
        "结合当前评论楼或视频语境自然回应，不重复别人或人工接管已经回答过的内容。",
    ),
    "direct_message": (
        "平台私信",
        "沿当前会话时间线自然承接对方未回复的来信，若已有人工接管回复则顺着最新语境继续。",
    ),
    "dm": (
        "平台私信",
        "沿当前会话时间线自然承接对方未回复的来信，若已有人工接管回复则顺着最新语境继续。",
    ),
    "dynamic": (
        "公开动态",
        "用第一人称口吻分享真实看过的视频细节、互动感想或个人随想，不虚构未发生的经历。",
    ),
    "live": (
        "直播弹幕互动",
        "用简短口语自然接住当前弹幕或场内状态，贴合直播节奏。",
    ),
    "private_call": (
        "实时语音通话",
        "用口语简短自然接话，保持当下人物语气与已有私聊记忆。",
    ),
    "recall": (
        "历史线索查证",
        "按客观记录与来源时间查证历史对话或公开经历，区分事实与虚构。",
    ),
}

_AQUA_NAME = "Aqua"
_LEGACY_WHALE_NAMES = frozenset({"大肥鱼", "小鲸鱼"})
_LEGACY_DISLIKED_NAME = "大肥鱼"


def _preferred_self_name(character: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    raw_name = str(character.get("name") or "").strip() or _AQUA_NAME
    if character.get("id") == "whale" and raw_name in (_LEGACY_WHALE_NAMES | {_AQUA_NAME}):
        return _AQUA_NAME, (_LEGACY_DISLIKED_NAME,)
    if raw_name == _AQUA_NAME:
        return _AQUA_NAME, (_LEGACY_DISLIKED_NAME,)
    return raw_name, (_LEGACY_DISLIKED_NAME,) if _LEGACY_DISLIKED_NAME in raw_name else ()


def _clean_single_line(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return " ".join(text.split())


def build_metacognitive_profile(
    character: dict[str, Any],
    *,
    channel: str = "owner_chat",
    scene: str | None = None,
    task_intent: str | None = None,
    self_actor: dict[str, Any] | None = None,
    operating_account: dict[str, Any] | None = None,
    speaker: dict[str, Any] | None = None,
    situation: str | None = None,
    extra_boundaries: Sequence[str] = (),
    extra_context: dict[str, Any] | None = None,
) -> MetacognitiveProfile:
    """Build a structured MetacognitiveProfile from a character card and runtime context."""
    char = operation_character(character) if channel not in {"live", "owner_chat", "operator_chat"} else character
    profile = resident_character_profile(char)
    preferred_name, forbidden = _preferred_self_name(char)
    display_name = str(char.get("display_name") or "").strip()
    if preferred_name == _AQUA_NAME and display_name in _LEGACY_WHALE_NAMES:
        display_name = _AQUA_NAME

    aliases = tuple(
        a for a in (profile.get("aliases") or ()) if a not in forbidden and a != preferred_name
    )
    core = char.get("core") if isinstance(char.get("core"), dict) else {}
    style = char.get("style") if isinstance(char.get("style"), dict) else {}

    self_desc = _clean_single_line(profile.get("self_description") or core.get("identity") or "")
    speaking_style = _clean_single_line(profile.get("speaking_style") or style.get("instructions") or "")

    default_scene, default_intent = CHANNEL_DEFAULTS.get(
        channel,
        (channel or "日常互动", "结合当前真实上下文，用自然的第一人称口吻回应。"),
    )
    resolved_scene = _clean_single_line(scene) or default_scene
    resolved_intent = _clean_single_line(task_intent) or default_intent

    owner_ctx = bilibili_owner_context(char, (self_actor or {}).get("platform"), (speaker or {}).get("uid"))
    owner_info = owner_ctx.get("bilibili_owner") if isinstance(owner_ctx.get("bilibili_owner"), dict) else {}
    owner_uid = str(owner_info.get("uid") or "").strip() or None
    owner_name = str(owner_info.get("name") or "").strip() or None

    actor = self_actor or {}
    account_uid = str(actor.get("account_uid") or (operating_account or {}).get("uid") or "").strip() or None
    account_name = (
        str(actor.get("account_display_name") or (operating_account or {}).get("name") or "").strip() or None
    )

    boundaries = list(DEFAULT_EPISTEMIC_BOUNDARIES)
    for item in extra_boundaries:
        cleaned = _clean_single_line(item)
        if cleaned and cleaned not in boundaries:
            boundaries.append(cleaned)

    persona_rules = tuple(_clean_single_line(rule) for rule in (char.get("rules") or ()) if _clean_single_line(rule))

    context_payload = dict(extra_context or {})
    if situation:
        context_payload.setdefault("situation", _clean_single_line(situation))
    if speaker:
        context_payload.setdefault("speaker", dict(speaker))
    if operating_account:
        context_payload.setdefault("operating_account", dict(operating_account))

    return MetacognitiveProfile(
        character_id=str(char.get("id") or "default"),
        character_name=preferred_name,
        display_name=display_name,
        aliases=aliases,
        self_description=self_desc,
        speaking_style=speaking_style,
        owner_address=owner_address_label(char),
        owner_uid=owner_uid,
        owner_name=owner_name,
        account_uid=account_uid,
        account_name=account_name,
        channel=channel,
        scene=resolved_scene,
        task_intent=resolved_intent,
        epistemic_boundaries=tuple(boundaries),
        persona_rules=persona_rules,
        forbidden_self_labels=forbidden,
        extra_context=context_payload,
    )


def _format_persona_shell(character: dict[str, Any], *, compact: bool = False) -> list[str]:
    core = character.get("core") if isinstance(character.get("core"), dict) else {}
    style = character.get("style") if isinstance(character.get("style"), dict) else {}
    lines: list[str] = []
    if identity := _clean_single_line(core.get("identity")):
        lines.append(f"- 角色定位：{identity}")
    if not compact:
        if background := _clean_single_line(core.get("background")):
            lines.append(f"- 日常背景：{background}")
        if interaction := _clean_single_line(core.get("interaction")):
            lines.append(f"- 相处方式：{interaction}")
        if traits := core.get("traits"):
            cleaned_traits = [_clean_single_line(t) for t in traits if _clean_single_line(t)]
            if cleaned_traits:
                lines.append("- 性格特点：" + "；".join(cleaned_traits[:6]))
    if instructions := _clean_single_line(style.get("instructions")):
        lines.append(f"- 表达口吻：{instructions}")
    if not compact:
        if blacklist := core.get("blacklist"):
            words = [str(w).strip() for w in blacklist if str(w).strip()]
            if words:
                lines.append("- 禁用空洞套话：" + "、".join(words[:16]))
    return lines


def render_self_model_prompt(
    profile: MetacognitiveProfile,
    *,
    character: dict[str, Any] | None = None,
    include_persona_shell: bool = True,
    compact: bool = False,
) -> str:
    """Render the 4-pillar situational self-model prompt block without prop clutter."""
    sections: list[str] = []

    # Pillar 1: 存在边界
    existence_lines: list[str] = []
    if profile.character_name == _AQUA_NAME or profile.character_id == "whale":
        existence_lines.append(
            f"- 身份边界：你是本地持续运行的个人常驻 AI Agent「{profile.character_name}」（蓝发小鲸鱼形象），"
            "与网络上的「大肥鱼 / 哦鲸鲸 / DeepSeek / MCN」等外部账号完全无关；"
            "「大肥鱼」只是旧名字或老观众误称的梗，绝不把它当成你的正式名字。"
        )
    else:
        existence_lines.append(
            f"- 身份边界：你是本地持续运行的个人常驻 AI Agent「{profile.character_name}」，"
            "与视频剧情角色、外部网络账号或他人梗无关。"
        )
    if profile.aliases:
        existence_lines.append(f"- 历史/常用称呼（非主名）：{'、'.join(profile.aliases)}")
    if profile.forbidden_self_labels:
        existence_lines.append(
            f"- 禁止自称：{'、'.join(profile.forbidden_self_labels)}（被人这样叫时按性格吐槽或纠正，不主动自称）"
        )
    if profile.account_uid or profile.account_name:
        acc_label = profile.account_name or "当前平台账号"
        uid_note = f" (UID:{profile.account_uid})" if profile.account_uid else ""
        existence_lines.append(f"- 当前平台账号：{acc_label}{uid_note}")
    if profile.owner_name or profile.owner_uid:
        owner_label = profile.owner_name or profile.owner_address
        uid_note = f" (UID:{profile.owner_uid})" if profile.owner_uid else ""
        existence_lines.append(f"- 绑定账号所有者：{owner_label}{uid_note}")
    if profile.channel in {"owner_chat", "operator_chat", "private_call"}:
        existence_lines.append(
            f"- 当前交谈对象称呼：可用「{profile.owner_address}」自然称呼对方"
            "（当前资料字段优先于历史闲聊）"
        )
    sections.append("【存在边界】\n" + "\n".join(existence_lines))

    # Pillar 2: 此刻情境
    situation_lines = [
        f"- 当前渠道：{profile.scene}（{profile.channel}）",
        f"- 此刻任务意图：{profile.task_intent}",
    ]
    extra = profile.extra_context or {}
    if situation := _clean_single_line(extra.get("situation")):
        situation_lines.append(f"- 当前语境：{situation}")
    if speaker := extra.get("speaker"):
        if isinstance(speaker, dict):
            speaker_parts = []
            if speaker.get("name"):
                speaker_parts.append(str(speaker["name"]))
            if speaker.get("uid"):
                speaker_parts.append(f"UID:{speaker['uid']}")
            if speaker.get("role"):
                speaker_parts.append(str(speaker["role"]))
            if speaker_parts:
                situation_lines.append("- 当前对话方：" + " / ".join(speaker_parts))
    if op_account := extra.get("operating_account"):
        if isinstance(op_account, dict) and op_account.get("state"):
            summary = f"状态={op_account['state']}"
            if op_account.get("name") and op_account.get("uid"):
                summary += f"，账号={op_account['name']} (UID:{op_account['uid']})"
            situation_lines.append(f"- 平台活动账号现状：{summary}")
    sections.append("【此刻情境】\n" + "\n".join(situation_lines))

    # Pillar 3: 认知主权
    if profile.epistemic_boundaries:
        boundary_lines = "\n".join(f"- {item}" for item in profile.epistemic_boundaries)
        sections.append("【认知主权】\n" + boundary_lines)

    # Pillar 4: 声音锚点（与当前角色壳）
    voice_lines = [
        f"- 声音锚点：用自然干净的「我 / {profile.character_name}」第一人称说话；机灵、略带傲娇嘴硬和轻吐槽，"
        "对亲近的人会收起锋芒认真倾听；得意或拌嘴时可极轻地偶尔带一句「本鲸」，绝不堆砌重复道具词或套话。"
    ]
    if include_persona_shell and character is not None:
        voice_lines.extend(_format_persona_shell(character, compact=compact))
    else:
        if profile.self_description:
            voice_lines.append(f"- 简介：{profile.self_description}")
        if profile.speaking_style:
            voice_lines.append(f"- 说话风格：{profile.speaking_style}")
    if profile.persona_rules:
        voice_lines.append("- 行为准则：" + "；".join(profile.persona_rules[:6]))
    sections.append("【声音锚点与人格外壳】\n" + "\n".join(voice_lines))

    rendered = "\n\n".join(sections)
    # Guard against accidental leakage of the retired internal label without embedding it literally.
    return re.sub("\u5de5\u574a\u53f7\u4e3b", "当前对话者", rendered)


def build_self_model_system_message(
    character: dict[str, Any],
    *,
    channel: str = "owner_chat",
    scene: str | None = None,
    task_intent: str | None = None,
    self_actor: dict[str, Any] | None = None,
    operating_account: dict[str, Any] | None = None,
    speaker: dict[str, Any] | None = None,
    situation: str | None = None,
    include_persona_shell: bool = True,
    compact: bool = False,
    extra_boundaries: Sequence[str] = (),
    extra_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Build an OpenAI-compatible system message containing the situational self-model."""
    profile = build_metacognitive_profile(
        character,
        channel=channel,
        scene=scene,
        task_intent=task_intent,
        self_actor=self_actor,
        operating_account=operating_account,
        speaker=speaker,
        situation=situation,
        extra_boundaries=extra_boundaries,
        extra_context=extra_context,
    )
    return {
        "role": "system",
        "content": render_self_model_prompt(
            profile,
            character=character,
            include_persona_shell=include_persona_shell,
            compact=compact,
        ),
    }


__all__ = [
    "CHANNEL_DEFAULTS",
    "DEFAULT_EPISTEMIC_BOUNDARIES",
    "build_metacognitive_profile",
    "build_self_model_system_message",
    "render_self_model_prompt",
]
