"""Verify the native chat rendering and supervise assistant bodies and their stops."""

import itertools
import random
from collections import Counter

from sft_dataset import bucket_for

IGNORE = -100
MARKER = "\ue000sft-boundary-"
ASSET_FIELDS = {"audio", "frames", "image", "images", "video", "pixel_values", "visual"}


def validated_messages(row, tokenizer):
    if row.get("modality", "text") != "text" or ASSET_FIELDS & row.keys():
        raise ValueError("Text SFT cannot consume visual or audio samples without a verified pixel/audio processor")
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) < 3 or len(messages) % 2 != 1:
        raise ValueError("Expected one system message and complete user/assistant pairs")
    roles = ["system", *itertools.islice(itertools.cycle(("user", "assistant")), len(messages) - 1)]
    content = []
    controls = [*tokenizer.all_special_tokens, "<think>", "</think>", MARKER]
    for role, message in zip(roles, messages, strict=True):
        if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] != role:
            raise ValueError("Unexpected role or multimodal message fields in text SFT")
        text = message["content"]
        if not isinstance(text, str) or not text.strip() or any(token and token in text for token in controls):
            raise ValueError(f"Empty, nontext, or template control content in {role} message")
        content.append({"role": role, "content": text.strip()})
    return content


def render_with_spans(tokenizer, messages):
    if not tokenizer.is_fast or not tokenizer.chat_template or tokenizer.eos_token_id is None:
        raise ValueError("SFT requires a fast tokenizer with its native chat template and EOS")

    def render(rows):
        return tokenizer.apply_chat_template(rows, tokenize=False, add_generation_prompt=False, enable_thinking=False)

    marked = []
    bounds = []
    for index, message in enumerate(messages):
        start = f"{MARKER}{index}-start\ue001"
        stop = f"{MARKER}{index}-stop\ue001"
        marked.append(message | {"content": start + message["content"] + stop})
        bounds.append((start, stop))

    native = render(messages)
    rest = render(marked)
    restored = ""
    assistant_spans = []
    for message, (start, stop) in zip(messages, bounds, strict=True):
        before, found_start, rest = rest.partition(start)
        body, found_stop, rest = rest.partition(stop)
        if not found_start or not found_stop or body != message["content"]:
            raise ValueError(f"Native template removed, reordered, or changed {message['role']} content")
        restored += before
        if message["role"] == "assistant":
            assistant_spans.append((len(restored), len(restored) + len(body)))
        restored += body
    if restored + rest != native:
        raise ValueError("Native template output changes around message boundaries")
    return native, assistant_spans


def encode_row(row, tokenizer, max_seq_length):
    text, spans = render_with_spans(tokenizer, validated_messages(row, tokenizer))
    encoded = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False)
    ids = encoded["input_ids"]
    offsets = encoded["offset_mapping"]
    if len(ids) > max_seq_length:
        raise ValueError(f"Sequence has {len(ids)} tokens, above {max_seq_length}; do not truncate answers")
    labels = [IGNORE] * len(ids)
    for left, right in spans:
        body = [index for index, (start, end) in enumerate(offsets) if start < right and end > left]
        if not body or offsets[body[0]][0] != left or offsets[body[-1]][1] != right:
            raise ValueError("Assistant body boundaries do not align with native tokens")
        if any(start < left or end > right for start, end in (offsets[index] for index in body)):
            raise ValueError("Assistant token crosses a role boundary")
        stop = next((i for i in range(body[-1] + 1, len(ids)) if offsets[i][1] > offsets[i][0]
                     and not text[slice(*offsets[i])].isspace()), None)
        if stop is None or ids[stop] != tokenizer.eos_token_id:
            raise ValueError("Assistant body lacks its native EOS stop")
        for index in (*body, stop):
            labels[index] = ids[index]
    count = sum(label != IGNORE for label in labels)
    if count <= len(spans) or labels[0] != IGNORE:
        raise ValueError("No usable causal assistant supervision")
    return {"input_ids": ids, "attention_mask": encoded["attention_mask"], "labels": labels}, count


def tokenize_split(rows, tokenizer, max_seq_length, *, allow_legacy=False):
    encoded = []
    for index, row in enumerate(rows, 1):
        try:
            try:
                bucket = bucket_for(row["category"])
            except ValueError:
                if not allow_legacy or "bucket" in row:
                    raise
                bucket = "legacy"
            if row.get("bucket", bucket) != bucket:
                raise ValueError("Category and training bucket differ")
            inputs, tokens = encode_row(row, tokenizer, max_seq_length)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{row.get('source', index)}: {error}") from error
        encoded.append((bucket, inputs, tokens))
    if not encoded:
        raise ValueError("Training and development splits cannot be empty")
    return encoded


def balance_training(rows, *, seed, global_batch):
    """Keep original train records, adding a deterministic minority-only replay."""
    rng = random.Random(seed)
    general, role = [], []
    for row in rows:
        (general if row[0] == "general_ability" else role).append(row)
    if not general or not role:
        raise ValueError("Token balancing requires both general and character records")
    rng.shuffle(general)
    rng.shuffle(role)
    general_cycle, role_cycle = itertools.cycle(general), itertools.cycle(role)
    output = list(rows)
    total = sum(row[2] for row in output)
    general_tokens = sum(row[2] for row in general)
    while abs(general_tokens / total - 0.3) > 0.03 or len(output) % global_batch:
        row = next(general_cycle if 10 * general_tokens < 3 * total else role_cycle)
        output.append(row)
        total += row[2]
        if row[0] == "general_ability":
            general_tokens += row[2]
    rng.shuffle(output)
    return output


def token_counts(rows):
    counts = Counter()
    for bucket, _, count in rows:
        counts[bucket] += count
    return dict(counts)
