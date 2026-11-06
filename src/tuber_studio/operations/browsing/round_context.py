"""Task-local ownership for browse rounds, retries, and generation resumes."""

import contextvars

read_round = contextvars.ContextVar("browsing_read_round", default=None)
generation_resume = contextvars.ContextVar("browsing_generation_resume", default=False)
discovery_retry = contextvars.ContextVar("browsing_discovery_retry", default=None)
agent_depth = contextvars.ContextVar("browsing_agent_depth", default=0)
