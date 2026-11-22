"""ContextVars and admission hooks shared by model callers without L3 scheduling imports."""

from contextvars import ContextVar

operation_lane = ContextVar("operation_lane", default=None)
operation_ready_at = ContextVar("operation_ready_at", default=None)
operation_ticket = ContextVar("operation_ticket", default=None)
operation_checkpoint = ContextVar("operation_checkpoint", default=None)


async def prepare_model_request():
    if context := operation_ticket.get():
        turns, ticket = context
        await turns.next_request(ticket)
    if checkpoint := operation_checkpoint.get():
        checkpoint()


def mark_model_request():
    if context := operation_ticket.get():
        turns, ticket = context
        turns.started(ticket)


async def defer_background_request(retry_after):
    context = operation_ticket.get()
    if not context or context[1].lane not in {"background", "priority"}:
        return False
    turns, ticket = context
    await turns.wait_for_admission(ticket, retry_after)
    return True
