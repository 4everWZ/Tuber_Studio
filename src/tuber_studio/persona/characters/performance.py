"""Shared, owner-controlled presentation preferences."""

from pydantic import StrictBool

from ...core.models import StrictModel


class Performance(StrictModel):
    pets_enabled: StrictBool = True
    avatar_enabled: StrictBool = False


def performance(store):
    return Performance.model_validate(store.get("performance", {})).model_dump()


def save_performance(store, body: Performance):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise ValueError("请选择要更新的演出开关")
    result = performance(store) | changes
    store.set("performance", result)
    return result

