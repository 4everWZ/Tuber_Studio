"""Owner-chat tools. Domain services own validation, persistence and readback."""

import re
import unicodedata
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt

from ..models import StrictModel
from .workshop_settings import read_settings, settings_catalog, update_settings

OWNER_CONTROL_REPLY = "本次请求涉及号主专属设置，请在号主设置页操作。本轮未修改设置或创建任务。"

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


def setting_topics():
    return {group["label"]: [field["title"] for field in group["fields"].values()]
            for group in settings_catalog().values()}


def front_routes():
    return {
        "要求改变号主专属控制，或同时要求修改可调项目和专属控制": ("restricted", None),
        "查询工坊能做什么或有哪些可调项目": ("workbench_capabilities", None),
        "查看当前宠物或列出现有形象": ("read_pet", None),
        "改变聊天宠物的样子、表情或显示状态": ("pet_settings", None),
        **{f"查看{label}当前的设置值": ("read_daily", group) for group, label in settings_menu().items()},
        **{f"调整{label}设置": ("daily_settings", group) for group, label in settings_menu().items()},
        "仅需当场业务查询或操作，读回状态即完成整项工作": ("direct_tool", None),
        "委派后台完成研究、持续工作或形成一份可保存和交付的成果": ("agent_task", None),
        "补充或变更先前交办": ("steer_task", None),
        "停止或取消一项已有后台交办": ("cancel_task", None),
        "只讨论或假设，不实际改变任何状态": ("discuss", None),
        "交办对象或要求尚待号主说明": ("clarify", None),
        "纯对话，回应就已满足要求": ("chat", None),
    }


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


async def daily_settings(runtime, group, choose, check_scope, *, source, read_only=False):
    catalog = settings_catalog()
    if group not in settings_menu():
        raise ValueError("请选择能力目录中的设置分组")
    entry = catalog[group]
    before = read_settings(runtime, group)
    fields = entry["fields"]
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
        keys = fields.keys()
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
    result = PetChoice(action="list") if read_only else PetChoice.model_validate(await choose(schema, {
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
