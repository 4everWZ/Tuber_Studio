"""The existing authenticated administration boundary owns all browsing controls."""

from fastapi import Body, Depends, Query


def register_browsing_api(app, browsing, require_admin):
    dependencies = [Depends(require_admin)]

    @app.get("/api/automation", dependencies=dependencies)
    async def status():
        return browsing.status()

    @app.put("/api/automation/settings", dependencies=dependencies)
    async def configure(data: dict = Body(...)):
        return await browsing.configure(data)

    @app.post("/api/automation/preflight", dependencies=dependencies)
    async def preflight():
        return await browsing.preflight(for_automation=True)

    @app.post("/api/automation/enable", dependencies=dependencies)
    async def enable(data: dict = Body(...)):
        if set(data) != {"enabled"}:
            raise ValueError("只接受enabled字段")
        return await browsing.set_enabled(data["enabled"])

    @app.post("/api/automation/round", dependencies=dependencies)
    async def run(data: dict = Body(default={})):
        if set(data) - {"preview", "video"} or data.get("preview", True) is not True:
            raise ValueError("手工入口只允许只读预览")
        return await browsing.run(preview=True, video=data.get("video"))

    @app.post("/api/automation/stop", dependencies=dependencies)
    async def stop(data: dict = Body(default={})):
        if set(data) - {"emergency"} or type(data.get("emergency", False)) is not bool:
            raise ValueError("emergency须为布尔值")
        return await browsing.stop(emergency=data.get("emergency", False))

    @app.post("/api/automation/reset-stop", dependencies=dependencies)
    async def reset():
        browsing.operations.configure({"emergency": False})
        return browsing.status()

    @app.get("/api/automation/tasks", dependencies=dependencies)
    async def tasks(status: str | None = Query(None)):
        return browsing.tasks(status)

    @app.get("/api/automation/rounds", dependencies=dependencies)
    async def rounds():
        return browsing.rounds()

    @app.get("/api/automation/inbox", dependencies=dependencies)
    async def inbox():
        return browsing.inbox()

    @app.get("/api/automation/intents", dependencies=dependencies)
    async def intents():
        return browsing.intents()

    @app.post("/api/automation/intents/{identity}/review", dependencies=dependencies)
    async def review(identity: str, data: dict = Body(...)):
        if set(data) - {"action", "text"}:
            raise ValueError("未知审核字段")
        return browsing.review(identity, data.get("action"), data.get("text"))

    @app.post("/api/automation/intents/{identity}/publish", dependencies=dependencies)
    async def publish(identity: str):
        return await browsing.publish(identity)

    @app.post("/api/automation/tasks/{identity}/reread", dependencies=dependencies)
    async def reread(identity: str):
        return await browsing.reread(identity)
