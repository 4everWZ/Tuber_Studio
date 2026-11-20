"""Owner-chat tools. Domain services own validation, persistence and readback."""

import re
import unicodedata
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt

from ..models import StrictModel
from .workshop_settings import read_settings, settings_catalog, update_settings

OWNER_CONTROL_REPLY = "这项权限可在号主设置页管理。"

# This checks authority against the original request, independently of the subset emitted by the model.
_CONTROL_NAME = re.compile(
    r"历史引用|引用(?:以前|过去|历史)的?私聊|引用(?:聊天|对话)?历史|长期记忆|记忆(?:功能|开关)|"
    r"(?:启用|禁用|关闭|开启|打开)记忆|历史记忆|模型(?:凭据|密钥|连接|地址)|网关(?:凭据|密钥|地址)|"
    r"角色(?:身份|绑定)|号主绑定|(?:解绑|重绑)号主|平台(?:凭据|授权)|自动(?:发帖|发布|发送|投稿)|运营权限|"
    r"history_enabled|memory_enabled|bilibili_owner|auto_publish|provider_credentials|api[_ -]?key",
    re.I,
)
_CONTROL_OPERATION = re.compile(
    r"关[闭掉]?|打开|开启|启用|禁用|停用|切换|解绑|绑定|设置|调整|更换|更改|修改|改[为成到]?|"
    r"读取|查看|查询|显示|提供|设[为成]|开关|(?:enable|disable|set|read|grant)|[=:：]", re.I,
)
_QUERY = re.compile(r"查|核对|查看|看看|告诉我|列出|读一下|是什么|多少|几[秒首]|多[高宽大]")
_CHANGE = re.compile(r"改[成为到]|调[成到整]|设置?为|换[成个]|收起|隐藏|显示|恢复默认|关闭|开启")
_HYPOTHETICAL = re.compile(r"如果|假如|要是|先别|不用|不要|能不能举例|怎么(?:才能|把|换|改|调|设|关|开)")
_CHOICES = re.compile(
    r"(?:能|可以)(?:直接)?(?:调|做|用)什么(?=(?:设置|工具|选项)|[啊呀呢吗吧\s]*(?:[。！？!?，,；;]|$))"
    r"|哪些(?:设置|工具|选项)(?:能|可|可以)"
)
_TASK_DELIVERY = re.compile(r"交给后台|稍后交付|持续(?:检查|跟进)|留(?:一)?份|写进|保存成|生成.{0,5}(?:报告|便笺|记录)")


def requests_owner_control(source):
    text = unicodedata.normalize("NFKC", source)
    return any(_CONTROL_NAME.search(clause) and _CONTROL_OPERATION.search(clause)
               for clause in re.split(r"[。！？；\n]", text))


class SettingChoice(StrictModel):
    action: Literal["read", "update", "clarify"]
    changes: dict[str, StrictBool | StrictInt | StrictFloat | str] = Field(default_factory=dict)


class PetChoice(StrictModel):
    action: Literal["list", "select", "default", "show", "hide", "clarify"]
    choice: int | None = Field(default=None, ge=1, strict=True)


def settings_menu():
    return {group: item["label"] for group, item in settings_catalog().items() if group != "pets"}


def _label_terms(label):
    return {label[i:i + 2] for i in range(len(label) - 1) if label[i:i + 2].isalnum()}


def named_setting_fields(group, source):
    entry = settings_catalog()[group]
    terms = {key: _label_terms(field["title"]) for key, field in entry["fields"].items()}
    common = _label_terms(entry["label"])
    for key, values in terms.items():
        common |= set().union(*(values & other for name, other in terms.items() if name != key))
    return tuple(key for key, values in terms.items() if any(term in source for term in values - common))


def setting_target(group, source, *, character, read_only):
    """Require the quoted object to name a setting domain or one of its fields."""
    if group == "pets":
        labels = ("宠物", "形象", "表情", "皮肤")
        if not read_only:
            labels += (character["name"],)
            if character["id"] == "whale":
                labels += ("鱼鱼", "鲸鲸", "小鲸鱼")
        return any(label in source for label in labels)
    entry = settings_catalog().get(group)
    if not entry:
        return False
    # Short field references such as 高度 are part of the actual setting label.
    # Shared framing such as 当前设置 or 直播 is insufficient to bind a group.
    shared = {"运营", "直播", "设置", "当前", "是否", "时间", "上限", "最多"}
    labels = [entry["label"], *(field["title"] for field in entry["fields"].values())]
    return any(term in source for label in labels for term in _label_terms(label) - shared)


def requested_setting(source, character, previous=None):
    """Recognize a concrete setting object and an affirmative operation in the source."""
    if _HYPOTHETICAL.search(source) or _TASK_DELIVERY.search(source):
        return None
    if _CHOICES.search(source):
        return "workbench_capabilities", None, True, ()
    change = _CHANGE.search(source)
    read = not change and bool(_QUERY.search(source))
    def matching_groups(text):
        return [group for group in ("pets", *settings_menu())
                if setting_target(group, text, character=character, read_only=not change)]

    # In 把 X 换成 Y, X is the setting object and Y is its requested value or source.
    groups = (matching_groups(source[:change.start()]) if change else []) or matching_groups(source)
    if not (change or read) and previous and previous["group"] in settings_menu() and re.search(r"[？?]|呢", source):
        group = previous["group"]
        if not (fields := named_setting_fields(group, source)):
            return None
        return "daily_settings", group, True, fields
    if not (change or read) or len(groups) != 1:
        return None
    group, = groups
    if group == "pets":
        return "pet_settings", None, read, ()
    return "daily_settings", group, read, named_setting_fields(group, source) if read else ()


def is_non_read_request(source):
    return bool(_CHANGE.search(source) or _HYPOTHETICAL.search(source) or _TASK_DELIVERY.search(source))


def capabilities(runtime):
    catalog = settings_catalog()
    lines = ["我可以当场查看或调整这些项目：", "- 宠物：显示、表情、已有形象。"]
    for group, entry in catalog.items():
        if group != "pets":
            labels = "、".join(field["title"] for field in entry["fields"].values())
            lines.append(f"- {entry['label']}：{labels}。")
    lines.append("我也能查运营活动、视频进度、直播与游戏状态及回执。")
    background = [runtime.tasks.tools[name] for name in
                  ("operation_status", "today_activity", "web_search", "web_page", "inspect_video", "delegate_text",
                   "live_status", "live_handoff", "game_status", "game_action") if name in runtime.tasks.tools]
    if background:
        lines.append("需要持续推进时，可以交办给后台。当前后台工具有：")
        lines.extend(f"- {tool.description}" for tool in background)
    return {"reply_text": "\n".join(lines), "reply_format": "markdown", "card": None}


async def daily_settings(runtime, group, choose, check_scope, *, source, read_only=False, selected_fields=()):
    catalog = settings_catalog()
    if group not in settings_menu():
        raise ValueError("请选择能力目录中的设置分组")
    entry = catalog[group]
    before = read_settings(runtime, group)
    fields = entry["fields"]
    if set(selected_fields) - fields.keys() or (selected_fields and not read_only):
        raise ValueError("查询字段不属于当前设置分组")
    schema = SettingChoice.model_json_schema()
    schema["properties"]["changes"] = {"type": "object", "properties": fields, "additionalProperties": False}
    result = SettingChoice(action="read") if read_only else SettingChoice.model_validate(await choose(schema, {
        "分组": entry["label"], "可用字段": fields, "当前值": before["values"],
        "操作": "仅查看选 read；明确要求修改选 update，只填写这次所需字段。信息不足或要求包含清单以外的修改，选 clarify。",
    }))
    check_scope()
    if requests_owner_control(source):
        return {"reply_text": OWNER_CONTROL_REPLY, "card": None}
    if result.action != "update" and result.changes:
        raise ValueError("查询或澄清不能附带设置修改")
    if result.action == "clarify":
        return {"reply_text": f"你想调整{entry['label']}的哪一项？可选：" + "、".join(field["title"] for field in fields.values()), "card": None}
    if result.action == "update":
        receipt = await update_settings(runtime, group, result.changes, scope=before["scope"])
        keys = result.changes.keys()
    else:
        receipt = read_settings(runtime, group)
        keys = selected_fields or fields.keys()
    changes = {key: receipt["values"][key] for key in keys}
    def display(value):
        if isinstance(value, bool):
            return "开启" if value else "关闭"
        return str(value)
    summary = "，".join(f"{fields[key]['title']}为{display(value)}" for key, value in changes.items())
    return {"reply_text": f"{entry['label']}当前设置：{summary}。", "card": None,
            "tool_receipt": {"action": result.action, "group": group, "values": changes}}


async def pet_settings(runtime, character_id, choose, check_scope, *, source, read_only=False):
    pets = runtime.pets
    check_scope()
    before = await pets.options(character_id)
    check_scope()
    items = before["items"]
    options = [{"choice": index, **{key: item[key] for key in ("title", "style", "emotions", "animated")}}
               for index, item in enumerate(items, start=1)]
    schema = PetChoice.model_json_schema()
    schema["properties"]["choice"] = {"anyOf": [{"enum": list(range(1, len(items) + 1))}, {"type": "null"}]} if items else {"type": "null"}
    quoted = {left or right for left, right in re.findall(r"「([^」]+)」|“([^”]+)”", source)}
    named = [item["choice"] for item in options if item["title"] in quoted]
    if read_only:
        result = PetChoice(action="list")
    elif (len(quoted) == 1 and len(named) == 1 and len(_CHANGE.findall(source)) == 1
          and not _HYPOTHETICAL.search(source)):
        result = PetChoice(action="select", choice=named[0])
    else:
        result = PetChoice.model_validate(await choose(schema, {
            "可选择的已有形象与表情": options, "显示中": before["pets_enabled"],
            "当前形象": (before.get("selected") or {}).get("title", "默认形象"),
            "操作": "列出或查看选 list；切换形象选 select，choice 填符合号主描述的清单序号；恢复默认选 default；"
                    "打开、关闭显示分别选 show、hide。选择形象与显示是独立操作；请求超出清单或不明确选 clarify。",
        }))
    check_scope()
    if requests_owner_control(source):
        return {"reply_text": OWNER_CONTROL_REPLY, "card": None}
    if (result.choice is not None) != (result.action == "select"):
        raise ValueError("仅选择已有形象时需要清单序号")
    if result.action == "select" and not 1 <= result.choice <= len(items):
        raise ValueError("形象不在当前选择范围")
    if result.action == "clarify":
        return {"reply_text": "要查看或更换已有形象，还是调整宠物显示？", "card": None}
    state = pets.state(character_id)
    if result.action != "list":
        if any(state.get(key) != before.get(key) for key in ("selected_asset_id", "pets_enabled", "revision")):
            raise ValueError("宠物设置已变化，请重新选择")
        if result.action in {"select", "default"}:
            asset_id = items[result.choice - 1]["asset_id"] if result.action == "select" else None
            state = await pets.select(character_id, asset_id)
        else:
            state = pets.set_visible(character_id, result.action == "show")
    title = (state.get("selected") or {}).get("title", "默认形象")
    visible = "显示" if state["pets_enabled"] else "隐藏"
    text = f"宠物当前{visible}，形象是「{title}」。"
    if result.action == "list" and items:
        text += "可用形象：" + "、".join(item["title"] for item in items) + "。"
    return {"reply_text": text, "card": None, "tool_receipt": {
        "group": "pets", "action": result.action, "character_id": state["character_id"], "pets_enabled": state["pets_enabled"],
        "selected_asset_id": state["selected_asset_id"], "title": title,
        "selected": ({key: state["selected"][key] for key in ("asset_id", "title", "style", "emotions", "animated")}
                     if state["selected"] else None),
    }}
