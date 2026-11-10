"""Source-family splits and immutable artifacts for OC training."""

import hashlib
import json
import random
import re
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher

SPLIT_FILES = ("sft_train.json", "sft_val.json")
MANIFEST_FILE = "sft_manifest.json"
BUCKETS = ("anime_width", "dafeiyu_depth", "general_ability")


def bucket_for(category):
    if category == "anime_general":
        return "anime_width"
    if category.startswith("dafeiyu_"):
        return "dafeiyu_depth"
    if category == "foundation_replay":
        return "general_ability"
    raise ValueError(f"Unknown OC training category {category!r}")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def normalized(text):
    return re.sub(r"[^\w]|_", "", unicodedata.normalize("NFKC", text).casefold())


def similar(left, right, threshold):
    return (
        min(len(left), len(right)) >= 8
        and 2 * min(len(left), len(right)) >= threshold * (len(left) + len(right))
        and SequenceMatcher(None, left, right, autojunk=False).ratio() >= threshold
    )


def near_turn(left, right):
    question, answer = left
    other_question, other_answer = right
    if question == other_question:
        return len(question) >= 8 or answer == other_answer
    return similar(question, other_question, 0.9) and similar(answer, other_answer, 0.85)


def group_rows(rows):
    """Connect explicit source aliases and shared exact or near-duplicate turns."""
    parents = list(range(len(rows)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def link(left, right):
        parents[find(left)] = find(right)

    aliases = {}
    indexed_turns = []
    for index, row in enumerate(rows):
        link(index, aliases.setdefault(row["source_group"], index))
        turns = [normalized(msg["content"]) for msg in row["messages"][1:]]
        pairs = list(zip(turns[::2], turns[1::2], strict=True))
        for other, existing in indexed_turns:
            if find(index) != find(other) and any(near_turn(a, b) for a in pairs for b in existing):
                link(index, other)
        indexed_turns.append((index, pairs))

    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[find(index)].append(row)
    for group in groups.values():
        aliases = sorted({row["source_group"] for row in group})
        group_id = digest(json_bytes(aliases))[:24]
        for row in group:
            row["group_id"] = group_id
    return sorted(groups.values(), key=lambda group: group[0]["group_id"])


def split_rows(rows, seed):
    groups = group_rows(rows)
    totals = Counter(row["bucket"] for row in rows)
    targets = {bucket: max(1, round(total * 0.12)) for bucket, total in totals.items()}
    dev = [row for group in groups if any(row.get("prefer_dev") for row in group) for row in group]
    remaining = [group for group in groups if not any(row.get("prefer_dev") for row in group)]
    counts = Counter(row["bucket"] for row in dev)
    rng = random.Random(seed)
    rng.shuffle(remaining)
    train = []
    for group in remaining:
        group_counts = Counter(row["bucket"] for row in group)
        candidate = counts + group_counts
        improves = sum(abs(target - candidate[bucket]) for bucket, target in targets.items()) < sum(
            abs(target - counts[bucket]) for bucket, target in targets.items()
        )
        if improves and all(candidate[bucket] < totals[bucket] for bucket in group_counts):
            dev.extend(group)
            counts = candidate
        else:
            train.extend(group)
    if not train or not dev or {row["bucket"] for row in train} != set(totals):
        raise ValueError("Source groups cannot make a nonempty, representative training split")
    return train, dev


def write_derived_dataset(output_dir, train, dev, source_hashes, seed):
    content = dict(zip(SPLIT_FILES, (json_bytes(train), json_bytes(dev)), strict=True))
    manifest = {
        "schema": "dafeiyu-grouped-sft-v1",
        "seed": seed,
        "source_sha256": source_hashes,
        "files_sha256": {name: digest(body) for name, body in content.items()},
        "splits": {
            name: {
                "examples": len(rows),
                "groups": len({row["group_id"] for row in rows}),
                "buckets": dict(Counter(row["bucket"] for row in rows)),
            }
            for name, rows in zip(SPLIT_FILES, (train, dev), strict=True)
        },
    }
    content[MANIFEST_FILE] = json_bytes(manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, body in content.items():
        path = output_dir / name
        if path.is_symlink() or (path.exists() and path.read_bytes() != body):
            raise FileExistsError(f"{name} already exists with different content; choose a new output directory")
    for name, body in content.items():
        path = output_dir / name
        if not path.exists():
            with path.open("xb") as output:
                output.write(body)
    return manifest


def read_split_files(data_dir, *, require_manifest):
    inputs = {name: (data_dir / name).read_bytes() for name in SPLIT_FILES}
    fingerprints = {name: digest(body) for name, body in inputs.items()}
    train, dev = [json.loads(body) for body in inputs.values()]
    manifest_path = data_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        if require_manifest:
            raise ValueError("Clean baseline requires a grouped SFT manifest")
        return train, dev, fingerprints
    manifest_data = manifest_path.read_bytes()
    manifest = json.loads(manifest_data)
    if manifest.get("schema") != "dafeiyu-grouped-sft-v1" or manifest.get("files_sha256") != fingerprints:
        raise ValueError("SFT split files do not match the grouped manifest")
    train_groups, dev_groups = ({row["group_id"] for row in split} for split in (train, dev))
    if not train_groups or not dev_groups or train_groups & dev_groups:
        raise ValueError("SFT train and development groups must be nonempty and disjoint")
    return train, dev, fingerprints | {MANIFEST_FILE: digest(manifest_data)}
