"""Situational metacognitive self-model prompt builder without duplicated instructions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ..core.agent_types import MetacognitiveProfile
from ..core.identity import bilibili_owner_context, owner_address_label
from ..core.models import resident_character_profile
from ..core.text import operation_character

DEFAULT_EPISTEMIC_BOUNDARIES: tuple[str, ...] = (
    "视角分清：看视频和逛评论区时像真实观众一样自然聊具体画面或细节，分清外部剧情、他人经历与自己，不把别人做过的事说成自己亲身经历，也不必机械套用固定起手词。",
    "独立判断：遇到假科普、离谱谣言或反串钓鱼时不顺从附和，先核实或轻巧拆台，不长篇说教。",
    "真实交付：没看过、没查到或没发出的内容绝不编造完成回执；直接输出自然纯文本，绝不输出 JSON 包装、方括号引用编号或内部字段名。",
)

PUBLIC_CHANNELS: frozenset[str] = frozenset(
    {
        "video_browsing",
        "video_comment",
        "top_level",
        "top_level_comment",
        "floor_reply",
        "comment_floor",
        "comment_draft",
        "comment_reply",
        "dynamic",
        "dynamic_post",
        "dynamic_reply",
    }
)

STRANGER_COMMENT_CHANNELS: frozenset[str] = frozenset(
    {
        "video_browsing",
        "video_comment",
        "top_level",
        "top_level_comment",
        "floor_reply",
        "comment_floor",
        "comment_draft",
        "comment_reply",
    }
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
        "结合字幕与画面抓最有趣或最戳人的具体细节，按各分支语境自然简短互动。",
    ),
    "video_comment": (
        "视频评论区（主评）",
        "像真实 B 站观众一样写 1-2 句自然短评（通常 15-55 字），抓一个具体细节或槽点直接开聊，别写复述全片的长评；没特别想说的就跳过。",
    ),
    "top_level": (
        "视频评论区（主评）",
        "像真实 B 站观众一样写 1-2 句自然短评（通常 15-55 字），抓一个具体细节或槽点直接开聊，别写复述全片的长评；没特别想说的就跳过。",
    ),
    "top_level_comment": (
        "视频评论区（主评）",
        "像真实 B 站观众一样写 1-2 句自然短评（通常 15-55 字），抓一个具体细节或槽点直接开聊，别写复述全片的长评；没特别想说的就跳过。",
    ),
    "floor_reply": (
        "评论楼内对话",
        "看清楼内时间线与回复指向（谁在对谁说话、是否有账号主人人工接管），像楼里顺手回一句那样简短自然（通常 10-50 字）接住对方的话头。",
    ),
    "comment_floor": (
        "评论楼内对话",
        "看清楼内时间线与回复指向（谁在对谁说话、是否有账号主人人工接管），像楼里顺手回一句那样简短自然（通常 10-50 字）接住对方的话头。",
    ),
    "comment_draft": (
        "评论区回复",
        "结合当前评论楼或视频语境，简短自然接住对方的话头（通常 10-50 字），不重复别人或人工接管已经回答过的内容。",
    ),
    "comment_reply": (
        "评论区回复",
        "结合当前评论楼或视频语境，简短自然接住对方的话头（通常 10-50 字），不重复别人或人工接管已经回答过的内容。",
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


def _is_public_channel(channel: str) -> bool:
    return (channel or "").strip().lower() in PUBLIC_CHANNELS


def _channel_voice_rule(profile: MetacognitiveProfile) -> str:
    name = profile.character_name or _AQUA_NAME
    ch = (profile.channel or "").strip().lower()
    if name == _AQUA_NAME or profile.character_id == "whale":
        if ch in STRANGER_COMMENT_CHANNELS:
            return "- 自称与口癖：默认用「我」说话，偶尔俏皮带一句「Aqua」「本鲸」或「本鲸鱼」（绝不自称「大肥鱼」，自然不连句复读）；在陌生 UP 主评论区尽量避开脚本常用起手词「本鱼」。"
        return "- 自称与口癖：默认用「我」说话，偶尔俏皮带一句「Aqua」「本鲸」或「本鲸鱼」（绝不自称「大肥鱼」）；在自己的动态、直播间和熟人私聊里偶尔用「本鱼」也完全自然。"
    return f"- 自称与口癖：日常自然用「我」或「{name}」，保持口语克制。"


def _select_channel_examples(
    character: dict[str, Any],
    *,
    channel: str,
    max_examples: int = 2,
) -> list[dict[str, str]]:
    core = character.get("core") if isinstance(character.get("core"), dict) else {}
    raw_examples = core.get("examples")
    if not isinstance(raw_examples, list) or max_examples <= 0:
        return []
    cleaned: list[dict[str, str]] = []
    for item in raw_examples:
        if not isinstance(item, dict):
            continue
        viewer = _clean_single_line(item.get("viewer"))
        reply = _clean_single_line(item.get("reply"))
        if viewer and reply:
            cleaned.append({"viewer": viewer, "reply": reply})
    if not cleaned:
        return []

    is_public = _is_public_channel(channel)
    pool = cleaned

    def _matches(item: dict[str, str], keywords: tuple[str, ...]) -> bool:
        joined = item["viewer"] + "\n" + item["reply"]
        return any(k in joined for k in keywords)

    chosen: list[dict[str, str]] = []
    if is_public:
        # Prioritize punchy short Bilibili comment examples over defensive anti-bait replies.
        preferred_groups = (
            ("卡扣", "翻车", "手工", "刺激", "吐槽"),
            ("罐子", "大米", "镜头", "笑死", "细节", "弹幕"),
        )
    else:
        preferred_groups = (
            ("累", "熬", "喝口水", "烦", "安慰", "陪"),
            ("夸", "反应真快", "哼", "傲娇", "Aqua"),
        )
    for keywords in preferred_groups:
        for item in pool:
            if item not in chosen and _matches(item, keywords):
                chosen.append(item)
                break
        if len(chosen) >= max_examples:
            break
    for item in pool:
        if len(chosen) >= max_examples:
            break
        if item not in chosen:
            chosen.append(item)
    return chosen[:max_examples]


def _format_persona_shell(
    character: dict[str, Any],
    *,
    profile: MetacognitiveProfile,
    compact: bool = False,
) -> list[str]:
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
                lines.append("- 性格切面：" + "；".join(cleaned_traits[:4]))
    if instructions := _clean_single_line(style.get("instructions")):
        lines.append(f"- 语气与称呼：{instructions}")
    lines.append(_channel_voice_rule(profile))
    if not compact:
        if blacklist := core.get("blacklist"):
            words = [str(w).strip() for w in blacklist if str(w).strip()]
            if words:
                lines.append("- 禁用空洞套话与刷屏词：" + "、".join(words[:20]))
        if selected_examples := _select_channel_examples(character, channel=profile.channel, max_examples=2):
            lines.append("- 参考对白（学语气和视角，勿照抄原句）：")
            for ex in selected_examples:
                lines.append(f"  - 问：{ex['viewer']} -> 答：{ex['reply']}")
    return lines


_BOUNDARY_TOPICS: tuple[tuple[str, ...], ...] = (
    ("第三人称", "当成自己", "亲身经历", "视角分清"),
    ("假消息", "谣言", "钓鱼", "反串", "苦情剧本"),
    ("JSON", "方括号引用", "内部字段名", "完成回执"),
)


def _norm_clause(text: str) -> str:
    return re.sub(r"[\s，,。！？!?；;：:「」『』“”\"'（）()【】\[\]—\-·/]+", "", text)


def _is_duplicate_boundary(rule: str, existing: Sequence[str]) -> bool:
    norm_rule = _norm_clause(rule)
    if not norm_rule:
        return True
    for item in existing:
        norm_item = _norm_clause(item)
        if norm_rule == norm_item or norm_rule in norm_item or norm_item in norm_rule:
            return True
    for markers in _BOUNDARY_TOPICS:
        if any(m in rule for m in markers) and any(any(m in item for m in markers) for item in existing):
            return True
    return False


def render_self_model_prompt(
    profile: MetacognitiveProfile,
    *,
    character: dict[str, Any] | None = None,
    include_persona_shell: bool = True,
    compact: bool = False,
) -> str:
    """Render concise situational metacognitive prompt without duplicated instructions."""
    sections: list[str] = []

    # Section 1: 身份与账号（动态元认知边界，避免与角色卡长段设定重复）
    existence_lines: list[str] = [f"- 当前角色：{profile.character_name}"]
    if profile.aliases:
        existence_lines.append(f"- 历史/常用称呼（非主名）：{'、'.join(profile.aliases)}")
    if profile.forbidden_self_labels:
        joined_labels = "、".join(profile.forbidden_self_labels)
        if _is_public_channel(profile.channel):
            existence_lines.append(
                f"- 禁用自称：{joined_labels}（仅当对方明确在直接称呼或 @ 本账号时才炸毛纠正自己叫「{profile.character_name}」；"
                "评论或视频里提到这些词多是在聊视频主角或别人外号，切勿对号入座）"
            )
        else:
            existence_lines.append(
                f"- 禁用自称：{joined_labels}（被人当面这样叫时炸毛纠正或接梗吐槽）"
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
    sections.append("【身份与账号】\n" + "\n".join(existence_lines))

    # Section 2: 此刻情境
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

    # Section 3: 判断与表达边界（无抽象黑话，合并边界与红线并去重）
    boundary_items = list(profile.epistemic_boundaries)
    if profile.persona_rules and not compact:
        for rule in profile.persona_rules[:4]:
            if not _is_duplicate_boundary(rule, boundary_items):
                boundary_items.append(rule)
    if boundary_items:
        sections.append("【判断与表达边界】\n" + "\n".join(f"- {item}" for item in boundary_items))

    # Section 4: 角色外壳与参考示例
    voice_lines: list[str] = []
    if include_persona_shell and character is not None:
        voice_lines.extend(_format_persona_shell(character, profile=profile, compact=compact))
    else:
        if profile.self_description:
            voice_lines.append(f"- 简介：{profile.self_description}")
        if profile.speaking_style:
            voice_lines.append(f"- 说话风格：{profile.speaking_style}")
        voice_lines.append(_channel_voice_rule(profile))
    sections.append("【角色外壳】\n" + "\n".join(voice_lines))

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
    "PUBLIC_CHANNELS",
    "STRANGER_COMMENT_CHANNELS",
    "build_metacognitive_profile",
    "build_self_model_system_message",
    "render_self_model_prompt",
]
