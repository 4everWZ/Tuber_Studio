"""Shared, owner-controlled presentation preferences."""

from fastapi import Depends
from pydantic import StrictBool

from .models import StrictModel


class Performance(StrictModel):
    pets_enabled: StrictBool = True
    avatar_enabled: StrictBool = False


def performance(store):
    return Performance.model_validate(store.get("performance", {})).model_dump()


def register_performance(app, runtime, require_admin):
    @app.get("/api/settings/performance", dependencies=[Depends(require_admin)])
    async def read():
        return performance(runtime.store)

    @app.put("/api/settings/performance", dependencies=[Depends(require_admin)])
    async def update(body: Performance):
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise ValueError("请选择要更新的演出开关")
        result = performance(runtime.store) | changes
        runtime.store.set("performance", result)
        return result
