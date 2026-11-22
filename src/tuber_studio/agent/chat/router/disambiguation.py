"""Deictic candidate disambiguation and deduplication helpers."""

import re

from ..models import (
    _DYNAMIC_ID,
    _DYNAMIC_URL,
    _MENTIONED_BV,
    _ORDINAL_REFERENCE,
    _SINGULAR_DEICTIC,
    TurnRoute,
)


def _unique_candidate_match(message: str, group: list[dict]) -> dict | None:
    """Return the unique candidate in a same-kind group explicitly identified by text/identity."""
    if _ORDINAL_REFERENCE.search(message):
        return None
    mentioned_bv = set(_MENTIONED_BV.findall(message))
    mentioned_dyn = set(_DYNAMIC_URL.findall(message) or _DYNAMIC_ID.findall(message))
    matched = []
    for item in group:
        identity = str(item.get("identity") or "")
        label = str(item.get("label") or "")
        if identity and (identity in mentioned_bv or identity in mentioned_dyn or identity in message):
            matched.append(item)
            continue
        title = label.split("｜", 1)[0].split("（", 1)[0].strip()
        if len(title) >= 3 and title in message:
            matched.append(item)
    return matched[0] if len(matched) == 1 else None


def _candidate_group_key(item: dict) -> str:
    """Group recalled sources by sub-type prefix (e.g. comment vs video) before disambiguation."""
    kind = str(item.get("kind") or "")
    if kind == "recalled_source":
        prefix = str(item.get("identity") or "").split(":", 1)[0]
        if prefix:
            return f"recalled_source:{prefix}"
    return kind


def _disambiguate_route(route: TurnRoute, message: str, candidates: list[dict]) -> TurnRoute:
    """Block guessing or bulk-selecting when a singular deictic spans >= 2 same-kind candidates."""
    if route.route not in {"instant_read", "direct_action", "prepare_publication"}:
        return route
    if route.action in {"clock", "operating_account", "operation_status", "workbench_capabilities", "activity"}:
        return route
    if not candidates or route.position is not None or _ORDINAL_REFERENCE.search(message):
        return route
    if not _SINGULAR_DEICTIC.search(message):
        return route
    if re.search(r"哪些|几[条件个位]|全部|所有|列表|都[要看看查发]|分别|各自", message):
        return route
    by_kind = {}
    for item in candidates:
        by_kind.setdefault(_candidate_group_key(item), []).append(item)
    multi_groups = {kind: group for kind, group in by_kind.items() if len(group) >= 2}
    if not multi_groups:
        return route
    if route.object_ref:
        selected = next((item for item in candidates if item["ref"] == route.object_ref), None)
        selected_kind = _candidate_group_key(selected) if selected is not None else None
        if selected is None or selected_kind not in multi_groups:
            return route
        group = multi_groups[selected_kind]
        if _unique_candidate_match(message, group) is not None:
            return route
        options = "\n".join(f"{idx}. {item['label']}" for idx, item in enumerate(group[:4], 1))
        return TurnRoute(route="clarify", action="clarify", question="你想指哪一个？\n" + options, clarification_id=route.clarification_id)
    # Multiple same-kind candidates and no object_ref / unique pin
    first_group = next(iter(multi_groups.values()))
    if _unique_candidate_match(message, first_group) is not None:
        return route
    options = "\n".join(f"{idx}. {item['label']}" for idx, item in enumerate(first_group[:4], 1))
    return TurnRoute(route="clarify", action="clarify", question="你想指哪一个？\n" + options, clarification_id=route.clarification_id)
