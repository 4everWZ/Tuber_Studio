"""Fake Bilibili platform doubles shared across operations, channel, and text tests."""

import asyncio

from tuber_studio.core.errors import PublishUncertain

BV = "BV1d8pU6xEZX"


class ControlledPlatform:
    def __init__(self):
        self.account = "100"
        self.calls = []
        self.sent = []
        self.feed = {"mention": [], "reply": []}
        self.no_subtitles = False
        self.delay = 0
        self.description = "完整的盐水实验简介"
        self.failure = None
        self.before_submit = None

    async def close(self):
        pass

    async def self_uid(self):
        return self.account

    async def discover(self, source, keyword="", page=1, limit=20):
        self.calls.append(("discover", source, keyword, page))
        return {
            "items": [{"bvid": BV, "aid": 42, "title": "盐水实验", "owner_uid": "200", "duration": 30}],
            "has_more": False,
        }

    async def video_metadata(self, video):
        self.calls.append(("metadata", video))
        await asyncio.sleep(self.delay)
        return {
            "bvid": video,
            "aid": 42,
            "title": "盐水实验",
            "description": self.description,
            "tags": ["科普"],
            "owner_uid": "200",
            "owner": "盐水实验室",
            "duration": 30,
            "parts": [{"cid": "11", "title": "第一P", "duration": 30}],
        }

    async def subtitles(self, bvid, cid, languages):
        self.calls.append(("subtitles", bvid, cid))
        if self.no_subtitles:
            from tuber_studio.operations.browsing.evidence import EvidenceUnavailable

            raise EvidenceUnavailable("no_subtitles", "无CC字幕")
        await asyncio.sleep(self.delay)
        return {
            "bvid": bvid,
            "cid": cid,
            "track_id": "track",
            "language": "zh-CN",
            "is_ai": True,
            "segments": [{"id": "0", "start": 0, "end": 10, "text": "加盐之后，鸡蛋浮了起来。"}],
            "coverage": {"complete": True},
        }

    async def fetch(self, video, max_pages=1):
        return {"video_id": video, "items": []}

    async def notifications(self, kind, cursor=None):
        return {"account_id": self.account, "items": self.feed[kind], "has_more": False, "cursor": None}

    async def read_public_thread(self, event):
        return {
            "account_id": self.account,
            "resource_type": "video",
            "target": {"type": 1, "oid": "42", "bvid": BV, "root": "501", "parent": "502"},
            "source": {"comment_id": "502", "author_uid": "300", "text": "大肥鱼你怎么看盐水里的鸡蛋？"},
            "items": [],
            "public_text": "",
        }

    async def publish_top_level(self, target, text, *, before_send):
        if self.before_submit:
            await self.before_submit()
        before_send()
        self.sent.append((target, text))
        if self.failure:
            raise self.failure
        return {"code": 0, "rpid": "900"}

    async def publish_resource_reply(self, target, text, *, before_send):
        return await self.publish_top_level(target, text, before_send=before_send)

    async def like_video(self, target, *, like=True, before_send=None):
        if before_send:
            before_send()
        self.sent.append((target, "like" if like else "unlike"))
        return {"code": 0, "action": "like" if like else "unlike", "video_id": target.get("bvid")}

    async def favorite_video(self, target, *, folder_id=None, before_send=None):
        if before_send:
            before_send()
        self.sent.append((target, "favorite"))
        return {"code": 0, "action": "favorite", "video_id": target.get("bvid")}

    async def verify_publication(self, target, receipt):
        return {"status": "not_found"}


class CommentPlatform:
    def __init__(self):
        self.sent = []
        self.failure = None

    async def close(self):
        pass

    async def popular(self):
        return [{"bvid": BV}]

    async def fetch(self, video, max_pages):
        return {"video_id": BV, "items": [{"comment_id": "123", "content": "鲸鱼娘怎么画？"}]}

    async def publish(self, video, comment, text):
        self.sent.append((video, comment, text))
        if self.failure:
            raise self.failure
        return {"rpid": 321}


class DMDelivery:
    def __init__(self):
        self.sent = []
        self.rows = []
        self.uncertain = False

    async def sessions(self):
        return "900", ["123"]

    async def history(self, uid):
        return self.rows

    async def send(self, uid, text, *, authorized=None):
        if authorized is not None and not authorized():
            raise ValueError("授权已关闭")
        self.sent.append((uid, text))
        if self.uncertain:
            raise PublishUncertain("test timeout")
        return {"msg_key": str(len(self.sent)), "receiver_id": uid}

    async def close(self):
        pass
