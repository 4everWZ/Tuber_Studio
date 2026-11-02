"""Administrator-only inbox, policy, source-bound preparation and delivery."""

from typing import Literal

from fastapi import Depends
from pydantic import Field

from .direct_messages import MessagePolicy
from .live_media import StrictModel


class ReviewMessage(StrictModel):
    action: Literal["approve", "reject", "edit", "copy"]
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    meme_id: str | None = Field(default=None, pattern=r"^meme_[a-f0-9]{32}$")


def register_message_api(app, messages, require_admin):
    auth = [Depends(require_admin)]

    @app.get("/api/messages", dependencies=auth)
    async def listing():
        return messages.list()

    @app.put("/api/messages/policy", dependencies=auth)
    async def policy(value: MessagePolicy):
        return await messages.configure(value.model_dump())

    @app.post("/api/messages/sync", dependencies=auth)
    async def sync():
        return await messages.sync()

    @app.get("/api/messages/conversation", dependencies=auth)
    async def conversation(platform: str, user_id: str):
        if len(platform) > 32 or len(user_id) > 100:
            raise ValueError("用户身份无效")
        return messages.conversation(platform, user_id)

    @app.post("/api/messages/inbox/{identity}/reply", dependencies=auth)
    async def prepare(identity: int):
        return {"draft_id": messages.enqueue("message", identity)}

    @app.post("/api/messages/drafts/{identity}/review", dependencies=auth)
    async def review(identity: int, value: ReviewMessage):
        options = {"meme_id": value.meme_id} if "meme_id" in value.model_fields_set else {}
        return messages.review(identity, value.action, value.text, **options)

    @app.post("/api/messages/drafts/{identity}/send", dependencies=auth)
    async def send(identity: int):
        return await messages.send(identity)

    @app.post("/api/messages/drafts/{identity}/retry", dependencies=auth)
    async def retry(identity: int):
        return messages.retry(identity)
