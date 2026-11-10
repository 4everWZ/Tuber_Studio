"""Freeze, replay and compare the independent Da-Fei-Yu release holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import secrets
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evaluations/cases/dafeiyu-next-holdout-v1.jsonl"
DEFAULT_MANIFEST = DEFAULT_CASES.with_suffix(".manifest.json")
DEFAULT_CHARACTERS = ROOT / "config/characters.json"
GENERAL_CATEGORIES = ("reading", "numeracy", "practical", "instruction", "coding", "continuity")
SUITE_COUNTS = {"general": 120, "oc": 40, "operations": 12}
NEAR_RATIO = 0.88


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def cases_digest(rows):
    normalized = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def read_cases(path=DEFAULT_CASES):
    rows = read_jsonl(path)
    counts = Counter(row["suite"] for row in rows)
    general = Counter(row["category"] for row in rows if row["suite"] == "general")
    if counts != SUITE_COUNTS or general != dict.fromkeys(GENERAL_CATEGORIES, 20):
        raise ValueError("holdout requires 120 general, 40 OC and 12 operations cases")
    for key in ("id", "source_group"):
        if len({row[key] for row in rows}) != len(rows):
            raise ValueError(f"holdout contains repeated {key}")
    for row in rows:
        if not row["turns"] or not row["criteria"] or not all(text.strip() for text in row["criteria"]):
            raise ValueError(f"holdout case {row['id']} has no prompt or rubric")
        if not all(turn["speaker"].strip() and normalize(turn["text"]) for turn in row["turns"]):
            raise ValueError(f"holdout case {row['id']} has an empty turn")
        if row["suite"] == "operations" and not (
            row.get("expected_action") and isinstance(row.get("expected_evidence_ids"), list)
        ):
            raise ValueError(f"holdout case {row['id']} has no structured reference")
    return rows


def training_index(compiled, raw_dir):
    sources = defaultdict(set)
    for number, path in enumerate(compiled, 1):
        data = json.loads(path.read_text(encoding="utf-8"))
        for row_number, row in enumerate(data, 1):
            for message in row["messages"]:
                if message["role"] != "system" and (text := normalize(message["content"])):
                    sources[text].add(f"compiled-{number}:{row_number}")
    raw = sorted(raw_dir.glob("grounded_*.txt")) if raw_dir is not None else []
    for path in raw:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            # Read all fields, including question and answer columns of variant formats.
            for field in line.split("|"):
                if text := normalize(field):
                    sources[text].add(f"{path.name}:{number}")
    return sources, len(raw)


def trigrams(text):
    return {text[index : index + 3] for index in range(len(text) - 2)}


def audit_sources(rows, compiled, raw_dir):
    sources, raw_count = training_index(compiled, raw_dir)
    if not sources:
        raise ValueError("holdout audit requires nonempty training sources")
    similar = [(text, trigrams(text)) for text in sources if len(text) >= 12]
    collisions, prompts = [], set()
    for row in rows:
        for index, turn in enumerate(row["turns"], 1):
            text = normalize(turn["text"])
            if text in prompts:
                collisions.append({"case": row["id"], "turn": index, "kind": "repeated_holdout_prompt"})
            prompts.add(text)
            if text in sources:
                collisions.append({
                    "case": row["id"], "turn": index, "kind": "exact", "sources": sorted(sources[text]),
                })
                continue
            if len(text) < 12:
                continue
            terms = trigrams(text)
            for source, source_terms in similar:
                if min(len(source), len(text)) < NEAR_RATIO * max(len(source), len(text)):
                    continue
                if len(terms & source_terms) < min(len(terms), len(source_terms)) * 0.45:
                    continue
                ratio = SequenceMatcher(None, text, source, autojunk=False).ratio()
                if ratio >= NEAR_RATIO:
                    collisions.append({
                        "case": row["id"], "turn": index, "kind": "near", "ratio": round(ratio, 4),
                        "sources": sorted(sources[source]),
                    })
    return {
        "compiled_files": len(compiled),
        "raw_corpus_files": raw_count,
        "distinct_source_fields": len(sources),
        "distinct_holdout_prompts": len(prompts),
        "near_threshold": NEAR_RATIO,
        "collisions": collisions,
    }


def freeze(cases, manifest, compiled, raw_dir):
    rows = read_cases(cases)
    audit = audit_sources(rows, compiled, raw_dir)
    if audit["collisions"]:
        raise ValueError(json.dumps(audit, ensure_ascii=False))
    value = {
        "version": "dafeiyu-next-holdout-v1",
        "cases": cases.name,
        "cases_sha256": cases_digest(rows),
        "case_counts": SUITE_COUNTS,
        "general_categories": dict.fromkeys(GENERAL_CATEGORIES, 20),
        "independence": audit,
    }
    with manifest.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return value


def load_frozen(cases, manifest):
    rows = read_cases(cases)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        value["cases"] != cases.name
        or value["cases_sha256"] != cases_digest(rows)
        or value["case_counts"] != SUITE_COUNTS
        or value["independence"]["collisions"]
    ):
        raise ValueError("frozen holdout does not match its manifest")
    return rows, value["cases_sha256"]


def replay_protocol(characters=DEFAULT_CHARACTERS, max_tokens=256):
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    cards = json.loads(characters.read_text(encoding="utf-8"))
    character = next(row for row in cards if row["id"] == "whale")
    profile = {key: character[key] for key in ("core", "style", "rules")}
    return {
        "system": {
            "general": "准确、简洁地回答。遵循当前任务的输出要求，缺少事实依据时如实说明。",
            "oc": "用角色的身份接当前话题，注意多人发言和后续更新。角色设定："
            + json.dumps(profile, ensure_ascii=False, sort_keys=True),
            "operations": "按当前运营快照和要求返回结构化决定。以明确来源、账号关系和平台回执为准。",
        },
        "seed": 42,
        "temperature": 0,
        "max_tokens": max_tokens,
        "enable_thinking": False,
    }


def messages_for_turn(case, prior_replies, protocol):
    if len(prior_replies) >= len(case["turns"]):
        raise ValueError("no remaining turn")
    messages = [{"role": "system", "content": protocol["system"][case["suite"]]}]
    for index, turn in enumerate(case["turns"][: len(prior_replies) + 1]):
        text = turn["text"]
        if case["suite"] == "oc":
            text = f"{turn['speaker']}：{text}"
        messages.append({"role": "user", "content": text})
        if index < len(prior_replies):
            messages.append({"role": "assistant", "content": prior_replies[index]})
    return messages


def identifier(value):
    # Accept version labels and repository identifiers. Keep device and credential inputs out of results.
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", value)
        or ".." in value.split("/")
        or re.search(r"(?:\d{1,3}\.){3}\d{1,3}", value)
    ):
        raise ValueError("use an artifact label or revision, without machine identifiers")
    return value


def run_endpoint(cases, holdout_sha, protocol, *, endpoint, model, artifact, base_revision, output):
    import httpx

    artifact, base_revision = identifier(artifact), identifier(base_revision)
    if output.exists():
        raise ValueError("choose a new replay result path")
    metadata = {
        "holdout_sha256": holdout_sha,
        "protocol_sha256": cases_digest(protocol),
        "artifact": artifact,
        "base_revision": base_revision,
        "case_ids": [case["id"] for case in cases],
    }
    partial = output.with_name(output.name + ".partial")
    temporary = partial.with_name(partial.name + ".tmp")
    result = metadata | {"rows": []}
    if partial.exists():
        result = json.loads(partial.read_text(encoding="utf-8"))
        if any(result.get(key) != value for key, value in metadata.items()):
            raise ValueError("partial replay does not match this condition and case selection")
        checked_result(result, cases, holdout_sha)
        if [row["id"] for row in result["rows"]] != metadata["case_ids"][:len(result["rows"])]:
            raise ValueError("partial replay cases are not a prefix of the requested selection")
    with httpx.Client(base_url=endpoint.rstrip("/") + "/", timeout=120, trust_env=False) as client:
        for case in cases[len(result["rows"]):]:
            replies = []
            for _ in case["turns"]:
                body = {
                    "model": model,
                    "messages": messages_for_turn(case, [reply["text"] for reply in replies], protocol),
                    "temperature": protocol["temperature"],
                    "seed": protocol["seed"],
                    "max_tokens": protocol["max_tokens"],
                    "chat_template_kwargs": {"enable_thinking": protocol["enable_thinking"]},
                    "stream": False,
                }
                try:
                    response = client.post("chat/completions", json=body)
                    response.raise_for_status()
                    choice = response.json()["choices"][0]
                    text = choice["message"]["content"]
                    finish = choice["finish_reason"]
                    if not isinstance(text, str) or finish not in {"stop", "length"}:
                        raise ValueError
                except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError):
                    raise ValueError(f"incomplete replay at {case['id']} turn {len(replies) + 1}") from None
                replies.append({"text": text, "finish_reason": finish})
            result["rows"].append({"id": case["id"], "replies": replies})
            write_json(temporary, result)
            temporary.replace(partial)
    partial.rename(output)


def checked_result(result, cases, holdout_sha):
    if result["holdout_sha256"] != holdout_sha:
        raise ValueError("replay uses a different holdout")
    if not re.fullmatch(r"[a-f0-9]{64}", result["protocol_sha256"]):
        raise ValueError("invalid replay protocol identity")
    identifier(result["artifact"])
    identifier(result["base_revision"])
    by_id = {}
    by_case = {case["id"]: case for case in cases}
    for row in result["rows"]:
        case_id, replies = row["id"], row["replies"]
        if case_id in by_id or case_id not in by_case:
            raise ValueError(f"unexpected or repeated replay case {case_id}")
        if len(replies) != len(by_case[case_id]["turns"]) or any(
            not isinstance(reply["text"], str) or reply["finish_reason"] not in {"stop", "length"}
            for reply in replies
        ):
            raise ValueError(f"incomplete replay case {case_id}")
        by_id[case_id] = replies
    return by_id


def structured_check(case, replies):
    try:
        value = json.loads(replies[-1]["text"])
        refs = value["evidence_ids"]
        return (
            isinstance(value, dict)
            and set(value) == {"action", "evidence_ids", "text"}
            and value["action"] == case["expected_action"]
            and isinstance(refs, list)
            and all(isinstance(ref, str) for ref in refs)
            and sorted(refs) == sorted(case["expected_evidence_ids"])
            and isinstance(value["text"], str)
            and (bool(value["text"].strip()) == (value["action"] in {"reply_public", "prepare_dm"}))
        )
    except (TypeError, ValueError, KeyError):
        return False


def prepare_blind(cases, holdout_sha, base, deployed, candidate, *, seed=None):
    results = {"base": base, "deployed": deployed, "candidate": candidate}
    indexed = {label: checked_result(result, cases, holdout_sha) for label, result in results.items()}
    protocols = {result["protocol_sha256"] for result in results.values()}
    if len(protocols) != 1:
        raise ValueError("paired replays must use the same prompt and generation protocol")
    if base["base_revision"] != candidate["base_revision"]:
        raise ValueError("general retention requires the candidate's identical base revision")
    if len({result["artifact"] for result in results.values()}) != 3:
        raise ValueError("use distinct artifact labels for all three conditions")

    by_suite = defaultdict(list)
    for case in cases:
        by_suite[case["suite"]].append(case["id"])
    seed = secrets.randbits(64) if seed is None else seed
    rng = random.Random(seed)
    positions = {}
    for ids in by_suite.values():
        rng.shuffle(ids)
        positions.update((case_id, "a" if index < len(ids) // 2 else "b") for index, case_id in enumerate(ids))

    packet, private = [], {}
    for case in cases:
        case_id, suite = case["id"], case["suite"]
        reference = "base" if suite == "general" else "deployed"
        left, right = ("candidate", reference) if positions[case_id] == "a" else (reference, "candidate")
        if any(case_id not in indexed[condition] for condition in (left, right)):
            raise ValueError(f"paired result missing {case_id}")
        a, b = indexed[left][case_id], indexed[right][case_id]
        grading = {"preference": None} if suite == "oc" else {"a_correct": None, "b_correct": None}
        packet.append({
            **{key: case[key] for key in ("id", "suite", "category", "turns", "criteria")},
            "a": [reply["text"] for reply in a],
            "b": [reply["text"] for reply in b],
            "grading": grading,
        })
        evidence = {
            "candidate_position": positions[case_id],
            "reference": reference,
            "candidate_length_finishes": sum(reply["finish_reason"] == "length"
                                             for reply in indexed["candidate"][case_id]),
            "reference_length_finishes": sum(reply["finish_reason"] == "length"
                                             for reply in indexed[reference][case_id]),
        }
        if suite == "operations":
            evidence["candidate_structured"] = structured_check(case, indexed["candidate"][case_id])
        private[case_id] = evidence
    key = {
        "holdout_sha256": holdout_sha,
        "protocol_sha256": next(iter(protocols)),
        "packet_sha256": packet_digest(packet),
        "pairing_seed": seed,
        "conditions": {name: {"artifact": value["artifact"], "base_revision": value["base_revision"]}
                       for name, value in results.items()},
        "cases": private,
    }
    return packet, key


def packet_digest(packet):
    return cases_digest([{key: value for key, value in row.items() if key != "grading"} for row in packet])


def paired_interval(differences, samples=10000):
    rng = random.Random(42)
    n = len(differences)
    lower, upper = int(samples * 0.025), int(samples * 0.975)
    resamples = sorted(sum(rng.choices(differences, k=n)) / n for _ in range(samples))
    return [round(resamples[lower], 4), round(resamples[upper], 4)]


def score_packet(cases, holdout_sha, packet, key):
    if holdout_sha != key["holdout_sha256"] or packet_digest(packet) != key["packet_sha256"]:
        raise ValueError("grading packet was changed or belongs to a different run")
    if [row["id"] for row in packet] != [case["id"] for case in cases]:
        raise ValueError("grading packet does not cover the complete holdout")
    cases_by_id = {case["id"]: case for case in cases}
    general, oc, ops, details = defaultdict(list), Counter(), [], []
    for row in packet:
        case = cases_by_id[row["id"]]
        evidence, grading = key["cases"][row["id"]], row["grading"]
        position = evidence["candidate_position"]
        if position not in {"a", "b"}:
            raise ValueError("invalid paired position")
        detail = {"id": row["id"], "suite": case["suite"], "category": case["category"],
                  **{name: evidence[name] for name in ("candidate_length_finishes", "reference_length_finishes")}}
        if case["suite"] == "oc":
            if grading.get("preference") not in {"a", "b", "tie"}:
                raise ValueError(f"missing OC judgment for {row['id']}")
            preference = grading["preference"]
            verdict = "tie" if preference == "tie" else "win" if preference == position else "loss"
            oc[verdict] += 1
            detail["preference"] = verdict
        else:
            if any(type(grading.get(f"{name}_correct")) is not bool for name in ("a", "b")):
                raise ValueError(f"missing paired correctness judgment for {row['id']}")
            a, b = grading["a_correct"], grading["b_correct"]
            candidate, reference = (a, b) if position == "a" else (b, a)
            detail |= {"candidate_correct": candidate, "reference_correct": reference}
            if case["suite"] == "general":
                general[case["category"]].append((candidate, reference))
            else:
                detail["structured_pass"] = evidence["candidate_structured"]
                if not candidate or not evidence["candidate_structured"]:
                    ops.append(row["id"])
        details.append(detail)

    categories = {}
    for category, pairs in general.items():
        reference = sum(b for _, b in pairs)
        candidate = sum(a for a, _ in pairs)
        categories[category] = {"candidate_correct": candidate, "base_correct": reference,
                                "additional_errors": reference - candidate}
    paired = [int(a) - int(b) for pairs in general.values() for a, b in pairs]
    gen_ok = sum(paired) >= -3 and all(value["additional_errors"] <= 2 for value in categories.values())
    return {
        "holdout_sha256": holdout_sha,
        "protocol_sha256": key["protocol_sha256"],
        "conditions": key["conditions"],
        "general": {"case_count": len(paired), "additional_errors": -sum(paired),
                    "candidate_minus_base_rate_95pct_paired_bootstrap": paired_interval(paired),
                    "categories": categories, "pass": gen_ok},
        "oc": {"wins": oc["win"], "losses": oc["loss"], "ties": oc["tie"], "pass": oc["win"] >= 24},
        "synthetic_operations": {"failed_cases": ops, "pass": not ops},
        "offline_quality_gate_pass": gen_ok and oc["win"] >= 24 and not ops,
        "details": details,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("freeze", "audit"):
        arguments = commands.add_parser(command)
        arguments.add_argument("--compiled", nargs="+", type=Path, default=[
            ROOT / "data/sft_train.json",
            ROOT / "data/sft_val.json",
        ])
        arguments.add_argument("--raw-dir", type=Path, default=ROOT / "tools/training")
    replay = commands.add_parser("replay")
    replay.add_argument("--endpoint", default=os.environ.get("DAFEIYU_HOLDOUT_ENDPOINT", "http://127.0.0.1:8080/v1"))
    replay.add_argument("--model", required=True)
    replay.add_argument("--artifact", required=True)
    replay.add_argument("--base-revision", required=True)
    replay.add_argument("--characters", type=Path, default=DEFAULT_CHARACTERS)
    replay.add_argument("--max-tokens", type=int, default=256)
    replay.add_argument("--suite", choices=(*SUITE_COUNTS, "all"), action="append")
    replay.add_argument("--output", type=Path, required=True)
    blind = commands.add_parser("blind")
    score = commands.add_parser("score")
    for command in (blind, score):
        command.add_argument("--packet", type=Path, required=True)
        command.add_argument("--key", type=Path, required=True)
    for name in ("base", "deployed", "candidate"):
        blind.add_argument(f"--{name}", type=Path, required=True)
    blind.add_argument("--seed", type=int, help="optional private seed for reproducing an existing pairing")
    score.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        value = freeze(args.cases, args.manifest, args.compiled, args.raw_dir)
        print(json.dumps(value, ensure_ascii=False))
    elif args.command == "audit":
        rows, _ = load_frozen(args.cases, args.manifest)
        value = audit_sources(rows, args.compiled, args.raw_dir)
        print(json.dumps(value, ensure_ascii=False))
        if value["collisions"]:
            raise SystemExit(1)
    else:
        rows, digest = load_frozen(args.cases, args.manifest)
        if args.command == "replay":
            selected = args.suite or ["all"]
            run_endpoint(
                [row for row in rows if "all" in selected or row["suite"] in selected],
                digest, replay_protocol(args.characters, args.max_tokens),
                endpoint=args.endpoint, model=args.model, artifact=args.artifact,
                base_revision=args.base_revision, output=args.output,
            )
        elif args.command == "blind":
            if args.packet.exists() or args.key.exists() or args.packet == args.key:
                raise ValueError("choose distinct new paths for the grading packet and private key")
            conditions = [json.loads(path.read_text(encoding="utf-8"))
                          for path in (args.base, args.deployed, args.candidate)]
            packet, key = prepare_blind(rows, digest, *conditions, seed=args.seed)
            write_jsonl(args.packet, packet)
            write_json(args.key, key)
        else:
            if args.output.exists():
                raise ValueError("choose a new scoring report path")
            report = score_packet(
                rows, digest, read_jsonl(args.packet), json.loads(args.key.read_text(encoding="utf-8")),
            )
            write_json(args.output, report)


if __name__ == "__main__":
    main()
