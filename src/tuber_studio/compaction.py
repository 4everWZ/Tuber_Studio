"""Local Codex compaction port, preserving its selection/reconstruction algorithm.

Adapted from openai/codex f021168bde5357009fceb0477835ad90bd54c17c:
core/src/compact.rs and utils/string/src/truncate.rs. Apache-2.0; see
third_party/openai-codex. Transport, storage and text-only item envelopes are local.
This is the public LOCAL summarization path, not OpenAI's opaque remote compactor.
"""

import asyncio
import copy
import random
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"
SUMMARIZATION_PROMPT = (PROMPTS / "compact.md").read_text(encoding="utf-8").strip()
SUMMARY_PREFIX = (PROMPTS / "summary-prefix.md").read_text(encoding="utf-8").strip()
COMPACT_USER_MESSAGE_MAX_TOKENS = 20_000


class ContextWindowExceeded(ValueError):
    pass


class IncompleteCompaction(ValueError):
    pass


def approx_token_count(text):
    # Deliberately retain the upstream algorithm for its 20K retention budget.
    # This is NOT used to measure the real model's prompt/window capacity.
    return (len(text.encode("utf-8")) + 3) // 4


def truncate_text(text, max_tokens):
    """Port plan_token_truncation: head/tail, UTF-8 boundaries, external marker."""
    raw = text.encode("utf-8")
    max_bytes = max_tokens * 4
    if len(raw) <= max_bytes:
        return text
    left, right = max_bytes // 2, max_bytes - max_bytes // 2
    prefix = raw[:left].decode("utf-8", errors="ignore")
    suffix = raw[len(raw) - right :].decode("utf-8", errors="ignore") if right else ""
    removed_tokens = (len(raw) - max_bytes + 3) // 4
    return prefix + f"…{removed_tokens} tokens truncated…" + suffix


def is_summary_message(message):
    return message["role"] == "user" and (
        message.get("kind") == "summary" or message["content"].startswith(SUMMARY_PREFIX + "\n")
    )


def collect_user_messages(items):
    return [
        item
        for item in items
        if item["role"] == "user" and item.get("kind", "viewer") == "viewer" and not is_summary_message(item)
    ]


def build_compacted_history(user_messages, summary_text, max_tokens=COMPACT_USER_MESSAGE_MAX_TOKENS):
    selected = []
    remaining = max_tokens
    if max_tokens > 0:
        for message in reversed(user_messages):
            if remaining == 0:
                break
            tokens = approx_token_count(message["content"])
            item = copy.deepcopy(message)
            if tokens > remaining:
                item["content"] = truncate_text(item["content"], remaining)
                item["source_complete"] = False
            selected.append(item)
            if tokens > remaining:
                break
            remaining = max(0, remaining - tokens)
        selected.reverse()
    return selected + [
        {
            "role": "user",
            "content": summary_text or "(no summary available)",
            "kind": "summary",
            "source_complete": True,
        }
    ]


def insert_initial_context_before_last_real_user_or_summary(history, initial_context):
    history = copy.deepcopy(history)
    summary_index = None
    insertion_index = None
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if item["role"] == "user" and item.get("kind", "viewer") == "viewer" and not is_summary_message(item):
            insertion_index = index
            break
        if item["role"] == "user" and summary_index is None:
            summary_index = index
    if insertion_index is None:
        insertion_index = summary_index if summary_index is not None else len(history)
    return history[:insertion_index] + copy.deepcopy(initial_context) + history[insertion_index:]


def limits(config):
    window = config["context_tokens"]
    return (
        min(config.get("auto_compact_token_limit", window * 9 // 10), window * 9 // 10),
        window * config.get("effective_context_window_percent", 95) // 100,
    )


def needs_compaction(total_tokens, config, carried_prefix_tokens=0):
    automatic, usable = limits(config)
    scope = config.get("auto_compact_token_limit_scope", "total")
    counted = max(0, total_tokens - carried_prefix_tokens) if scope == "body_after_prefix" else total_tokens
    return counted >= automatic or total_tokens >= usable


def wire_messages(items):
    return [{"role": item["role"], "content": item["content"]} for item in items]


async def compact_history(
    history,
    prefix,
    initial_context,
    provider,
    *,
    max_retries=3,
    retry_base_seconds=0.2,
    summary_prompt=SUMMARIZATION_PROMPT,
):
    """Clone → append trigger → summarize → rebuild → reinject; commit is caller-owned.

    Context overflow removes one oldest item and resets retries. Interrupted and
    incomplete generations fail without installing history. Other retryable transport
    errors retry with bounded exponential backoff. Each response attempt is isolated.
    """
    original = copy.deepcopy(history)
    work = copy.deepcopy(history) + [{"role": "user", "content": summary_prompt, "kind": "compact_trigger"}]
    retries = 0
    while True:
        rendered = prefix + insert_initial_context_before_last_real_user_or_summary(work, initial_context)
        try:
            summary = await provider.summarize(wire_messages(rendered))
            break
        except ContextWindowExceeded:
            if len(work) <= 1:
                raise
            work.pop(0)
            retries = 0
        except (asyncio.CancelledError, IncompleteCompaction):
            raise
        except Exception:
            if retries >= max_retries:
                raise
            retries += 1
            # Codex async-utils/backoff.rs: 200ms * 2^(attempt-1), ±10% jitter,
            # floored to milliseconds. Retry count, not an invented delay cap, bounds it.
            delay_ms = int(retry_base_seconds * 1000 * 2 ** (retries - 1) * random.uniform(0.9, 1.1))
            await asyncio.sleep(delay_ms / 1000)
    if not isinstance(summary, str) or not summary.strip():
        raise IncompleteCompaction("模型没有返回完整压缩摘要；原上下文保留")
    replacement = build_compacted_history(collect_user_messages(original), SUMMARY_PREFIX + "\n" + summary)
    # The caller persists only logical history, re-rendering canonical context at
    # this exact same insertion boundary on every request, including after restart.
    return replacement
