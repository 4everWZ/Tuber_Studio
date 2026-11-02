"""Authenticated recovery controls reuse each channel's actual source checks."""

from typing import Literal

from fastapi import Body, Depends


def register_operation_recovery_api(app, runtime, channels, require_admin):
    dependencies = [Depends(require_admin)]

    @app.get("/api/operations/recovery-policy", dependencies=dependencies)
    async def policy():
        return {"max_recoveries": runtime.operations.policy()["max_recoveries"]}

    @app.put("/api/operations/recovery-policy", dependencies=dependencies)
    async def configure(data: dict = Body(...)):
        if set(data) != {"max_recoveries"} or type(data["max_recoveries"]) is not int:
            raise ValueError("只接受整数max_recoveries")
        value = runtime.operations.configure(data)
        return {"max_recoveries": value["max_recoveries"]}

    @app.post("/api/operations/continuations/{channel}/{local_id}/resume", dependencies=dependencies)
    async def resume(
        channel: Literal["comments", "messages", "dynamics", "browsing"],
        local_id: str,
        data: dict = Body(...),
    ):
        if (
            set(data) != {"continuation_id"}
            or not isinstance(data["continuation_id"], str)
            or len(data["continuation_id"]) > 100
        ):
            raise ValueError("需要当前来源的continuation_id")
        service = channels[channel]
        handler = getattr(service, "resume_continuation", None)
        if handler is None:
            raise ValueError("该渠道尚无恢复任务入口")
        return await handler(local_id, data["continuation_id"])
