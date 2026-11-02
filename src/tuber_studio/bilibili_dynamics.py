"""Single-attempt text or one-image dynamics, using the current adapter/client.

Payload: bilibili-api-python 17.4.2, dynamic.send_dynamic / send.instant.
Source distribution: https://pypi.org/project/bilibili-api-python/17.4.2/
Optional catalog image preparation; no challenge solving or endpoint fallback.
"""

import asyncio
import re

import httpx

from .bilibili import _ERRORS, PublishUncertain, _data, _positive_id
from .bilibili_images import upload_image
from .operation_text import validate_operation_text
from .publication_errors import platform_rejection


class PublishCancelled(ValueError):
    """A synchronous final authorization check cancelled a not-yet-sent write."""


class PublishPreparationFailed(ValueError):
    """Image preparation failed before a dynamic creation POST was attempted."""


class PublishPreparationCancelled(asyncio.CancelledError):
    """Cancellation before the final creation request, safe to review again."""


def validate_text(text):
    text = validate_operation_text(text, label="动态正文")
    if re.search(r"\b(?:memory|reply):\d+\b|\b(?:source_ids|fictional)\s*[=:]", text, re.IGNORECASE):
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


async def publish_image(adapter, text, image, *, before_send=None, prepare_authorized=None):
    text = validate_text(text)
    if not (adapter._sessdata and adapter._csrf):
        raise ValueError("发布动态需要先配置账号登录凭证")
    try:
        prepared = await upload_image(adapter, image, authorized=prepare_authorized)
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
                {"img_src": prepared["url"], "img_width": prepared["width"], "img_height": prepared["height"]}
            ],
            "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            "attach_card": None,
        }
    }
    return await _create(adapter, params, body, before_send=before_send)


async def _create(adapter, params, body, *, before_send):
    # No suspension between final authorization and starting the sole POST.
    if before_send is not None:
        before_send()
    try:
        response = await adapter.client.post(
            "https://api.bilibili.com/x/dynamic/feed/create/dyn",
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
