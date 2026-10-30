"""Single-attempt text dynamics, using the current comments adapter/client.

Payload: bilibili-api-python 17.4.2, dynamic.send_dynamic / send.instant.
Source distribution: https://pypi.org/project/bilibili-api-python/17.4.2/
No media upload, generated fingerprint, challenge solving or endpoint fallback.
"""

import re

import httpx

from .bilibili import PublishUncertain, _data, _positive_id


class PublishCancelled(ValueError):
    """A synchronous final authorization check cancelled a not-yet-sent write."""


def validate_text(text):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 500:
        raise ValueError("动态正文须为1到500字")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
        raise ValueError("动态正文含无效控制字符")
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
        _data(payload)
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
