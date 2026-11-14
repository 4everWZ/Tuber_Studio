"""Single-attempt text or one-image dynamics, using the current adapter/client.

Payload: bilibili-api-python 17.4.2, dynamic.send_dynamic / send.instant.
Source distribution: https://pypi.org/project/bilibili-api-python/17.4.2/
Optional catalog image preparation; no challenge solving or endpoint fallback.
"""

import asyncio
import re

import httpx

from ..operation_text import validate_operation_text
from ..publication_errors import platform_rejection
from .bilibili import _ERRORS, PublishUncertain, _data, _positive_id
from .bilibili_images import bfs_image_path, upload_image


class PublishCancelled(ValueError):
    """A synchronous final authorization check cancelled a not-yet-sent write."""


class PublishPreparationFailed(ValueError):
    """Image preparation failed before a dynamic creation POST was attempted."""


class PublishPreparationCancelled(asyncio.CancelledError):
    """Cancellation before the final creation request, safe to review again."""


def validate_text(text):
    text = validate_operation_text(text, label="动态正文")
    if re.search(r"\b(?:memory|reply|incident):\d+\b|\b(?:source_ids|fictional)\s*[=:]", text, re.IGNORECASE):
        raise ValueError("动态正文不能包含内部来源编号或技术元数据")
    return text.strip()


async def publish_text(adapter, text, *, before_send=None):
    """Never cache adapter/credentials: caller resolves Comments.adapter per send."""
    text = validate_text(text)
    if not (adapter._sessdata and adapter._csrf):
        raise ValueError("发布动态需要先配置账号登录凭证")
    # Standard public WBI signing is a read-only preparation step. Its failure
    # cannot mean an uncertain write because the POST has not happened yet.
    params = await adapter.wbi_params({"csrf": adapter._csrf})
    body = {
        "dyn_req": {
            "content": {"contents": [{"raw_text": text, "biz_id": "", "type": 1}]},
            "scene": 1,
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "attach_card": None,
        }
    }
    return await _create(adapter, params, body, before_send=before_send)


async def publish_image(adapter, text, image, *, before_send=None, prepare_authorized=None, on_prepared=None):
    text = validate_text(text)
    if not (adapter._sessdata and adapter._csrf):
        raise ValueError("发布动态需要先配置账号登录凭证")
    try:
        images = image if isinstance(image, (list, tuple)) else [image]
        if not 1 <= len(images) <= 9:
            raise ValueError("动态配图数量须为 1 至 9 张")
        prepared = []
        for asset in images:
            prepared.append(await upload_image(adapter, asset, authorized=prepare_authorized))
        params = await adapter.wbi_params({"csrf": adapter._csrf})
    except PublishCancelled:
        raise
    except asyncio.CancelledError:
        raise PublishPreparationCancelled from None
    except (ValueError, httpx.HTTPError):
        raise PublishPreparationFailed("图片准备失败，尚未提交动态；保留待审正文和图片") from None
    body = {
        "dyn_req": {
            "content": {"contents": [{"raw_text": text, "biz_id": "", "type": 1}]},
            "scene": 2,
            "pics": [
                {"img_src": item["url"], "img_width": item["width"], "img_height": item["height"]}
                for item in prepared
            ],
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "attach_card": None,
        }
    }
    if on_prepared is not None:
        on_prepared(prepared)
    return await _create(adapter, params, body, before_send=before_send)


async def read_dynamic_caption(adapter, identifier, account):
    params = await adapter.wbi_params({"id": identifier, "platform": "web", "features": "itemOpusStyle"})
    data = await adapter._get("/x/polymer/web-dynamic/v1/detail", params)
    try:
        item = data["item"]
        modules = item["modules"]
        if _positive_id(item["id_str"]) != identifier or _positive_id(modules["module_author"]["mid"]) != account:
            raise ValueError("这条动态不属于当前账号")
        if item["type"] not in {"DYNAMIC_TYPE_DRAW", "DYNAMIC_TYPE_WORD"}:
            raise ValueError("仅支持编辑本工作台发布的文字或普通图文动态")
        dynamic = modules["module_dynamic"]
        major = dynamic.get("major") or {}
        opus = major.get("opus") or {}
        if dynamic.get("additional") or dynamic.get("topic") or opus.get("title"):
            raise ValueError("这条动态包含需要在 B 站保留的组件，请在原动态编辑")
        summary = opus.get("summary") or dynamic.get("desc") or {}
        text = summary["text"]
        if not isinstance(text, str):
            raise ValueError("无法读取原动态正文")
        raw = opus.get("pics", []) if opus else (major.get("draw") or {}).get("items", [])
        pictures = [
            {"img_src": entry.get("url") or entry["src"], "img_width": entry["width"], "img_height": entry["height"]}
            for entry in raw
        ]
        if (item["type"] == "DYNAMIC_TYPE_DRAW" and not pictures) or any(
            not bfs_image_path(pic["img_src"])
            or type(pic["img_width"]) is not int or pic["img_width"] <= 0
            or type(pic["img_height"]) is not int or pic["img_height"] <= 0
            for pic in pictures
        ):
            raise ValueError("无法核对原动态的配图")
    except (AttributeError, TypeError, KeyError) as error:
        raise ValueError("无法读取原动态的正文与配图") from error
    return {"text": text, "pictures": pictures}


async def edit_dynamic_caption(adapter, identifier, text, pictures, *, before_send):
    if not (adapter._sessdata and adapter._csrf):
        raise ValueError("编辑动态需要有效的账号登录凭证")
    text = validate_text(text)
    info = await adapter._get("/x/dynamic/feed/edit/infos", {"dyn_id": identifier, "scene": 1, "platform": "web"})
    remaining = info.get("remain_edit_times")
    if type(remaining) is not int or remaining < 1:
        raise ValueError("B 站未确认这条动态还有可用编辑次数")
    if info.get("attach_card") or (info.get("commercial") or {}).get("commercial_entity_id"):
        raise ValueError("这条动态包含需要在 B 站保留的组件，请在原动态编辑")
    params = await adapter.wbi_params({"csrf": adapter._csrf, "platform": "web"})
    request = {
        "content": {"contents": [{"raw_text": text, "biz_id": "", "type": 1}]},
        "scene": 2 if pictures else 1,
        "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
        "attach_card": None,
    }
    if pictures:
        request["pics"] = pictures
    payload = await _submit(
        adapter, "edit", params, {"dyn_id_str": identifier, "dyn_req": request}, before_send=before_send,
    )
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise PublishUncertain("编辑响应缺少有效结果，请核对原动态")


async def _create(adapter, params, body, *, before_send):
    payload = await _submit(adapter, "create", params, body, before_send=before_send)
    try:
        data = _data(payload)
        identifier = _positive_id(data.get("dyn_id_str") or data.get("dyn_id"))
    except ValueError:
        raise PublishUncertain("平台未返回有效动态回执，请人工核对") from None
    return {
        "platform": "bilibili",
        "code": 0,
        "dynamic_id": identifier,
        "url": f"https://t.bilibili.com/{identifier}",
    }


async def _submit(adapter, action, params, body, *, before_send):
    # No suspension between final authorization and starting the sole POST.
    if before_send is not None:
        before_send()
    try:
        response = await adapter.client.post(
            f"https://api.bilibili.com/x/dynamic/feed/{action}/dyn",
            params=params,
            json=body,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise PublishUncertain("动态写入结果不确定，请到平台核对，禁止重试") from None
    # Only an explicit platform rejection is a definite failure. Malformed success
    # or missing receipt could follow an accepted write and must stay uncertain.
    if isinstance(payload, dict) and type(payload.get("code")) is int and payload["code"] != 0:
        raise platform_rejection(payload, fallback=_ERRORS.get(payload["code"], "平台明确拒绝了本次动态"))
    return payload
