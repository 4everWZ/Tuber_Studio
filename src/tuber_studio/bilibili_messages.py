"""Account-scoped text messaging, checked against published SDK 17.4.2 session.py.

Only fixed HTTPS endpoints and existing login credentials; no challenge fallback.
A single POST must return msg_key to establish delivery. Reads do not mark read.
"""

import asyncio
import json
import time

import httpx

from .bilibili import BilibiliAdapter, PublishUncertain, _data, _positive_id

ORIGIN = "https://api.vc.bilibili.com"


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
        data = await self._get(
            ORIGIN + "/svr_sync/v1/svr_sync/fetch_session_msgs",
            {"talker_id": _positive_id(uid), "session_type": 1, "size": 30, "begin_seqno": 0},
        )
        rows = data.get("messages") or []
        if not isinstance(rows, list):
            raise ValueError("平台私信消息记录无效")
        return rows[:30]

    async def send(self, uid, text, *, authorized=None):
        if not self._sessdata or not self._csrf:
            raise ValueError("发送私信需要 SESSDATA 和 bili_jct")
        receiver = _positive_id(uid)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
            raise ValueError("私信内容须为1到1000字")
        sender = await self.self_uid()
        if receiver == sender:
            raise ValueError("不能向当前账号自己发送自动私信")
        params = await self.wbi_params({"w_sender_uid": sender, "w_receiver_id": receiver})
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
                    "msg[msg_type]": 1,
                    "msg[msg_status]": 0,
                    "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
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
            raise PublishUncertain("私信发送结果不确定，请核对平台；禁止自动重发") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise PublishUncertain("私信响应没有有效回执，禁止自动重发")
        if payload["code"] != 0:
            _data(payload)
        try:
            key = _positive_id(payload["data"]["msg_key"])
        except (KeyError, TypeError, ValueError):
            raise PublishUncertain("私信响应缺少消息ID，禁止自动重发") from None
        return {"platform": "bilibili", "msg_key": key, "receiver_id": receiver, "sender_id": sender}
