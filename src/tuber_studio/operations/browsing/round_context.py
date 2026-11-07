"""Task-local ownership for browse rounds and worker depth."""

import contextvars

read_round = contextvars.ContextVar("browsing_read_round", default=None)
agent_depth = contextvars.ContextVar("browsing_agent_depth", default=0)
