"""Bounded settings for authenticated, owner-requested direct chat actions."""

from pydantic import StrictBool, TypeAdapter, ValidationError

from ..direct_messages import MessagePolicy
from ..live.media import MusicSettings, Scene
from ..live.options import LiveOptions
from ..operation_continuations import content_hash
from ..operations.browsing.models import BrowsingPolicy
from ..performance import Performance

# These are opt-in projections. Adding a provider or UI setting does not grant model access.
_GROUPS = {
    "pets": ("宠物展示", Performance, {
        "pets_enabled": "显示宠物",
    }),
    "browsing": ("运营取材与评论配图", BrowsingPolicy, {
        "interval_seconds": "轮间等待秒数", "notification_interval_seconds": "通知检查间隔秒数",
        "daily_watch_limit": "每日观看额度，0 表示不限", "business_timezone": "业务日时区",
        "candidate_limit": "每批候选上限", "max_reads": "每轮读取额度",
        "min_duration_seconds": "最短视频秒数", "max_duration_seconds": "最长视频秒数",
        "recent_sample_limit": "近期样本数", "frame_sampling": "取帧方式",
        "use_memes": "使用表情包", "generate_images": "生成配图",
    }),
    "messages": ("私信草稿配图", MessagePolicy, {
        "use_memes": "使用表情包", "generate_images": "生成配图",
    }),
    "dynamics": ("动态草稿配图", None, {
        "use_memes": "使用表情包", "generate_images": "生成配图",
    }),
    "live": ("直播显示", LiveOptions, {"timezone": "直播时区"}),
    "scene": ("直播画布", Scene, {
        "background": "背景样式", "width": "画布宽度", "height": "画布高度",
    }),
    "music": ("直播点歌", MusicSettings, {
        "volume": "音量", "queue_limit": "队列上限", "cooldown_seconds": "点歌冷却秒数",
        "request_ttl_seconds": "点歌有效秒数",
    }),
}


def _group(name):
    if not isinstance(name, str) or name not in _GROUPS:
        raise ValueError("请选择清单中的日常设置分组")
    return _GROUPS[name]


def _adapter(model, key):
    return TypeAdapter(model.model_fields[key].rebuild_annotation() if model else StrictBool)


def settings_catalog():
    """Return only supported fields, labels, scalar types and UI validation bounds."""
    return {group: {
        "label": label,
        "fields": {key: _adapter(model, key).json_schema() | {"title": title} for key, title in fields.items()},
    } for group, (label, model, fields) in _GROUPS.items()}


def _values(runtime, group):
    if group == "pets":
        return runtime.pets.state(runtime.character["id"])
    if group == "browsing":
        return runtime.browsing.policy()
    if group == "messages":
        return runtime.browsing.messages.policy()["base"]
    if group == "dynamics":
        return runtime.browsing.dynamics.policy()["base"]
    if group == "live":
        return runtime.live.options().model_dump()
    if group == "scene":
        return runtime.live.media.scene()
    if group == "music":
        return runtime.live.media.config()
    raise ValueError("未知设置分组")


def read_settings(runtime, group):
    _, _, fields = _group(group)
    current = _values(runtime, group)
    values = {key: current[key] for key in fields}
    scope = content_hash([runtime.tasks.identity(), group, values])
    return {"group": group, "values": values, "scope": scope}


async def update_settings(runtime, group, changes, *, scope):
    """Apply a direct owner's explicitly requested patch against the observed settings."""
    _, model, fields = _group(group)
    if not isinstance(changes, dict) or not changes or changes.keys() - fields.keys():
        raise ValueError("本次修改含有不支持的字段或缺少明确设置，未保存")
    if read_settings(runtime, group)["scope"] != scope:
        raise ValueError("设置或当前号主、账号、角色已变化，请重新读取")
    try:
        patch = {key: _adapter(model, key).validate_python(value, strict=True) for key, value in changes.items()}
    except ValidationError:
        raise ValueError("设置值的类型或范围不符，请按能力清单填写") from None
    if group == "pets":
        runtime.pets.set_visible(runtime.character["id"], patch["pets_enabled"])
    elif group == "browsing":
        await runtime.browsing.configure(patch)
    elif group == "messages":
        await runtime.browsing.messages.configure(_values(runtime, group) | patch)
    elif group == "dynamics":
        await runtime.browsing.dynamics.set_policy(patch)
    elif group == "live":
        runtime.live.set_options(LiveOptions.model_validate(_values(runtime, group) | patch))
    elif group == "scene":
        runtime.live.media.save_scene(_values(runtime, group) | patch)
    elif group == "music":
        settings = MusicSettings.model_validate(_values(runtime, group) | patch)
        runtime.live.media.save_music_settings(settings, runtime.character)
    return read_settings(runtime, group)
