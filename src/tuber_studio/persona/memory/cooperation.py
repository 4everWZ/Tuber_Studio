"""Source-bound evidence for applying a private turn as a standing work agreement."""

import re
from typing import Literal

from pydantic import Field

from ...core.models import StrictModel


class RoutineProposal(StrictModel):
    statement: str = Field(min_length=1, description="包含全部适用条件和要求的原话，逐字摘录。")
    scope_quote: str = Field(min_length=1, description="原话中表明跨次适用、通用惯例或稳定个人偏好的文字。")
    practice_quote: str = Field(min_length=1, description="原话中工坊能够实际执行的具体合作方式。")


class RoutineReview(StrictModel):
    meaning: Literal["号主本人的持续协作约定", "单次请求或邀请", "寒暄玩笑或举例转述", "缺少跨次范围或具体做法"]
    scope_supported: bool = Field(strict=True, description="范围摘录在整段号主原话中确实表示重复适用或稳定惯例。")
    practice_supported: bool = Field(strict=True, description="做法摘录写明工坊可以执行的协作要求。")
    same_agreement: bool = Field(strict=True, description="该范围修饰该做法，所选原句保留条件、例外和否定，属于号主本人。")


def _span(text, fragment):
    if not fragment.strip() or text.count(fragment) != 1:
        return None
    start = text.index(fragment)
    return [start, start + len(fragment)]


def locate_rule(text, proposal):
    """Bind distinct, substantive spans to a single unambiguous original statement."""
    if proposal is None:
        return None
    statement = _span(text, proposal.statement)
    scope = _span(proposal.statement, proposal.scope_quote)
    practice = _span(proposal.statement, proposal.practice_quote)
    if (not statement or not scope or not practice or scope == practice
            or not all(re.search(r"\w", part) for part in (proposal.scope_quote, proposal.practice_quote))):
        return None
    return {"statement": proposal.statement, "statement_span": statement,
            "scope_span": [statement[0]+i for i in scope], "practice_span": [statement[0]+i for i in practice]}


def corroborates(text, rule, review):
    return bool(bound_statement(text, rule) and review.meaning == "号主本人的持续协作约定"
                and review.scope_supported and review.practice_supported and review.same_agreement)


def with_scope_context(text, rule):
    """Bind the qualifying clause around the selected words, bounded by the instruction itself."""
    start, stop = rule["scope_span"]
    left, right = rule["statement_span"]
    practice_start, practice_stop = rule["practice_span"]
    if practice_start >= start:
        right = min(right, max(stop, practice_start))
    if practice_stop <= stop:
        left = max(left, min(start, practice_stop))
    delimiters = "，,。.：:；;！？!?\n"
    while start > left and text[start - 1] not in delimiters:
        start -= 1
    while stop < right and text[stop] not in delimiters:
        stop += 1
    if [start, stop] == rule["practice_span"]:
        start, stop = rule["scope_span"]
    return rule | {"scope_span": [start, stop], "proposed_scope_span": rule["scope_span"]}


def bound_statement(text, rule):
    """Validate persisted ranges against the complete owner quote on read."""
    if not isinstance(rule, dict):
        return None
    fields = ["statement_span", "scope_span", "practice_span"]
    if "proposed_scope_span" in rule:
        fields.append("proposed_scope_span")
    for field in fields:
        bounds = rule.get(field)
        if (not isinstance(bounds, list) or len(bounds) != 2 or not all(type(i) is int for i in bounds)
                or not 0 <= bounds[0] < bounds[1] <= len(text)):
            return None
    statement = text[slice(*rule["statement_span"])]
    original_scope = rule.get("proposed_scope_span", rule["scope_span"])
    proposal = RoutineProposal(statement=statement, scope_quote=text[slice(*original_scope)],
                               practice_quote=text[slice(*rule["practice_span"])])
    verified = locate_rule(text, proposal)
    if verified and "proposed_scope_span" in rule:
        verified = with_scope_context(text, verified)
    return statement if verified == rule else None


def active_owner_text(row):
    edited = row["proof"].get("owner_edit_at", 0) > row["source"]["at"]
    return row["anecdote"] if edited else row["quote"]


def collaboration_text(row):
    """Respect explicit owner saves. Old automatic turns have no model corroboration."""
    text = active_owner_text(row)
    proof = row["proof"]
    if (row["source"]["key"].startswith(("settings:", "confirmed_voice:", "task:")) or proof.get("owner_confirmed")
            or proof.get("owner_edit_at", 0) > row["source"]["at"]):
        return text
    if "cooperation_rule" in proof:
        return bound_statement(text, proof["cooperation_rule"])
    if not row["source"]["key"].startswith("turn:") or proof.get("inspection_only"):
        return None
    # Conservative compatibility for old, already classified turns only. New admissions
    # never pass through this recognizer. Management still exposes other original notes.
    if (re.fullmatch(r"(?:以后|今后)[^。！？；\n]{0,24}(?:先(?:讲|说|看)|都用)[^。！？；\n]+[。]?", text)
            and not re.search(r"玩笑|这次|今天|他说|她说|假如|不是真的|别记", text)):
        return text
    return None


def applicable_chat_memory(row):
    return row["memory_kind"] != "private" or row["proof"].get("scene") or bool(collaboration_text(row))
