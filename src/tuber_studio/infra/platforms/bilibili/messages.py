"""Account-scoped text messaging, checked against published SDK 17.4.2 session.py.

Only fixed HTTPS endpoints and existing login credentials; no challenge fallback.
A single POST must return msg_key to establish delivery. Reads do not mark read.
"""

import asyncio
import json
import time

import httpx

from ....core.errors import PublishUncertain, platform_rejection
from ....core.text import validate_operation_text
from .client import _ERRORS, BilibiliAdapter, _positive_id
from .images import upload_image

ORIGIN = "https://api.vc.bilibili.com"


def message_sequence(value):
    if type(value) is int or isinstance(value, str) and value.isascii() and value.isdecimal():
        number = int(value)
        if 0 < number < 2**63:
            return number
    return 0


class MessagePreparationCancelled(asyncio.CancelledError):
    """No final message submission occurred; the reviewed draft can be retained."""


class BilibiliMessagesAdapter(BilibiliAdapter):
    async def self_uid(self):
        if not self._sessdata:
            raise ValueError("读取私信需要配置账号 SESSDATA")
        nav = await self._get("/x/web-interface/nav", {})
        return _positive_id(nav.get("mid"))

    async def sessions(self):
        uid = await self.self_uid()
        data = await self._get(
            ORIGIN + "/session_svr/v1/session_svr/get_sessions",
            {"session_type": 4, "size": 20, "build": 0, "mobi_app": "web"},
        )
        rows = data.get("session_list")
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError("平台私信会话列表无效")
        return uid, [
            _positive_id(row.get("talker_id"))
            for row in rows[:20]
            if isinstance(row, dict) and row.get("session_type") == 1
        ]

    async def history(self, uid):
        return (await self.history_page(uid))[0]

    async def history_page(self, uid, *, before=0):
        params = {"talker_id": _positive_id(uid), "session_type": 1, "size": 30, "begin_seqno": 0}
        if before:
            params["end_seqno"] = str(before)
        data = await self._get(
            ORIGIN + "/svr_sync/v1/svr_sync/fetch_session_msgs",
            params,
        )
        rows = data.get("messages") or []
        if not isinstance(rows, list) or len(rows) > 30 or any(not isinstance(row, dict) for row in rows):
            raise ValueError("平台私信消息记录无效")
        sequences = [message_sequence(row.get("msg_seqno")) for row in rows]
        available = [value for value in sequences if value]
        if (data.get("has_more") and not available) or (
            before and available and max(available) >= int(before)
        ):
            raise ValueError("平台私信分页序列缺失或未推进")
        return rows, min(available) if data.get("has_more") else 0, max(available, default=0)

    async def reconcile_publication(self, target, *, author_uid, text, started_at, receipt=None):
        from .reconciliation import reconcile_message

        return await reconcile_message(
            self._get, target, author_uid=author_uid, text=text, started_at=started_at, receipt=receipt
        )

    async def send(self, uid, text, *, authorized=None, expected_sender=None):
        text = validate_operation_text(text, label="私信内容")
        return await self._send(uid, {"content": text}, authorized=authorized, expected_sender=expected_sender)

    async def send_image(self, uid, image, *, authorized=None, prepare_authorized=None, on_uploaded=None, expected_sender=None):
        return await self._send(
            uid, image, image_message=True, authorized=authorized, prepare_authorized=prepare_authorized,
            on_uploaded=on_uploaded, expected_sender=expected_sender,
        )

    async def _send(self, uid, content, *, image_message=False, authorized=None, prepare_authorized=None,
                    on_uploaded=None, expected_sender=None):
        if not self._sessdata or not self._csrf:
            raise ValueError("发送私信需要 SESSDATA 和 bili_jct")
        receiver = _positive_id(uid)
        try:
            sender = await self.self_uid()
            if expected_sender is not None and sender != expected_sender:
                raise ValueError("当前发送账号与已审核 UID 不符")
            if receiver == sender:
                raise ValueError("不能向当前账号自己发送自动私信")
            if image_message:
                content = await upload_image(self, content, authorized=prepare_authorized)
                if on_uploaded:
                    on_uploaded(content)
            params = await self.wbi_params({"w_sender_uid": sender, "w_receiver_id": receiver})
        except asyncio.CancelledError:
            raise MessagePreparationCancelled from None
        if authorized is not None and not authorized():
            raise ValueError("私信发送授权已停止，保留草稿")
        try:
            response = await self.client.post(
                ORIGIN + "/web_im/v1/web_im/send_msg",
                params=params,
                data={
                    "msg[sender_uid]": sender,
                    "msg[receiver_id]": receiver,
                    "msg[receiver_type]": 1,
                    "msg[msg_type]": 2 if image_message else 1,
                    "msg[msg_status]": 0,
                    "msg[content]": json.dumps(content, ensure_ascii=False),
                    "msg[dev_id]": "A6716E9A-7CE3-47AF-994B-F0B34178D28D",
                    "msg[new_face_version]": 0,
                    "msg[timestamp]": int(time.time()),
                    "from_filework": 0,
                    "build": 0,
                    "mobi_app": "web",
                    "csrf": self._csrf,
                    "csrf_token": self._csrf,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, asyncio.CancelledError) as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            raise PublishUncertain("私信发送结果待核对") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("B 站未返回有效的私信回执")
        if payload["code"] != 0:
            raise platform_rejection(
                payload,
                fallback=_ERRORS.get(payload["code"], "平台明确拒绝了本次私信"),
                secret_values=(self._sessdata, self._csrf),
            )
        try:
            key = _positive_id(payload["data"]["msg_key"])
        except (KeyError, TypeError, ValueError):
            raise PublishUncertain("B 站回执缺少私信消息 ID") from None
        return {"platform": "bilibili", "msg_key": key, "receiver_id": receiver, "sender_id": sender}
