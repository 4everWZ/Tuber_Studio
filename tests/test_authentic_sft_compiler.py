import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

TRAINING = Path(__file__).resolve().parents[1] / "tools" / "training"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(TRAINING))
    return tuple(
        importlib.import_module(name)
        for name in ("build_authentic_sft_dataset", "sft_dataset", "sft_tokenization", "run_model_queue")
    )


def test_compiler_preserves_source_roles_and_isolates_groups(modules, tmp_path):
    compiler, dataset, _, _ = modules
    sources = {name: (TRAINING / name).read_bytes() for name in compiler.SOURCE_NAMES}
    manifest = compiler.build_dataset(TRAINING, tmp_path)
    assert manifest == compiler.build_dataset(TRAINING, tmp_path)
    train, dev, _ = dataset.read_split_files(tmp_path, require_manifest=True)
    assert {row["group_id"] for row in train}.isdisjoint(row["group_id"] for row in dev)
    assert {row["bucket"] for row in train} == set(dataset.BUCKETS)
    assert {row["bucket"] for row in dev} == set(dataset.BUCKETS)
    rows = train + dev
    transfer = next(
        row for row in rows if row["source"].startswith("grounded_anime_transfer_dialogues.txt:2")
    )
    assert transfer["messages"][0]["content"].startswith("你是冬枝")
    assert transfer["messages"][1]["content"].startswith("约好来排练")
    assert transfer["messages"][2]["content"].startswith("我先在对面坐着")
    continuation = next(
        row for row in rows if row["source"].startswith("grounded_chatanime_continuations.txt:")
    )
    history = next(
        row
        for row in rows
        if row["source"].startswith("grounded_deepthink_dialogues.txt:")
        and row["source_group"] == continuation["source_group"]
    )
    assert continuation["messages"][:-2] == history["messages"]
    assert continuation["group_id"] == history["group_id"]
    assert all((TRAINING / name).read_bytes() == body for name, body in sources.items())


def test_near_duplicate_turn_and_mono_multi_are_one_group(modules):
    compiler, dataset, _, _ = modules
    question = "今晚这个排练用白色旗子还是蓝色旗子？"
    reply = "你拿蓝色旗子，白色旗子留给场边工作人员。"
    single = compiler.sample("one", "known-family", "anime_general", [(question, reply)])
    multi = compiler.sample(
        "two", "known-family", "anime_general", [(question, reply), ("出场了吗？", "快啦。")]
    )
    near = compiler.sample(
        "three", "other", "anime_general", [(question.replace("今晚", "今天"), reply + "好吧。")]
    )
    distinct = compiler.sample("four", "another", "anime_general", [("刚刚谁在楼下等你？", "邻居。")])
    assert sorted(map(len, dataset.group_rows([single, multi, near, distinct]))) == [1, 3]


def test_compiler_rejects_overwrite_of_existing_output(modules, tmp_path):
    compiler, _, _, _ = modules
    original = tmp_path / "sft_train.json"
    original.write_bytes(b"[]")
    with pytest.raises(FileExistsError):
        compiler.build_dataset(TRAINING, tmp_path)
    assert original.read_bytes() == b"[]"
    assert list(tmp_path.iterdir()) == [original]


class CharacterTokenizer:
    is_fast = True
    chat_template = "native"
    eos_token_id = 9999
    all_special_tokens = ["<eos>"]

    def __init__(self, *, drop_system=False, add_stop=True, emitted_stop_id=9999):
        self.drop_system = drop_system
        self.add_stop = add_stop
        self.emitted_stop_id = emitted_stop_id

    def apply_chat_template(self, messages, **kwargs):
        return "".join(
            f"[{message['role']}]\n"
            + ("<think>\n</think>\n" if message["role"] == "assistant" else "")
            + message["content"]
            + ("<eos>" if self.add_stop else "")
            + "\n"
            for message in messages
            if not self.drop_system or message["role"] != "system"
        )

    def __call__(self, text, **kwargs):
        ids, offsets = [], []
        i = 0
        while i < len(text):
            stop = text.startswith("<eos>", i)
            length = 5 if stop else 1
            ids.append(self.emitted_stop_id if stop else ord(text[i]))
            offsets.append((i, i + length))
            i += length
        return {"input_ids": ids, "offset_mapping": offsets, "attention_mask": [1] * len(ids)}


def test_mask_only_keeps_assistant_body_and_eos(modules):
    compiler, _, tokens, _ = modules
    row = compiler.sample(
        "source", "family", "dafeiyu_core", [("Q1", "first"), ("Q2", "second")], system="SYS"
    )
    encoded, count = tokens.encode_row(row, CharacterTokenizer(), 256)
    supervised = [label for label in encoded["labels"] if label != -100]
    assert supervised == [*map(ord, "first"), 9999, *map(ord, "second"), 9999]
    assert count == len(supervised)


def test_mask_accepts_a_model_declared_turn_end(modules):
    compiler, _, tokens, _ = modules
    row = compiler.sample("source", "family", "dafeiyu_core", [("Q", "A")], system="SYS")
    tokenizer = CharacterTokenizer(emitted_stop_id=7000)
    with pytest.raises(ValueError, match="native EOS"):
        tokens.encode_row(row, tokenizer, 256)
    encoded, count = tokens.encode_row(row, tokenizer, 256, end_tokens=(7000,))
    assert [label for label in encoded["labels"] if label != -100] == [ord("A"), 7000]
    assert count == 2


def test_mask_fails_closed_on_system_drop_missing_stop_cutoff_and_pixels(modules):
    compiler, _, tokens, _ = modules
    row = compiler.sample("source", "family", "dafeiyu_core", [("question", "answer")], system="SYS")
    with pytest.raises(ValueError, match="system content"):
        tokens.encode_row(row, CharacterTokenizer(drop_system=True), 256)
    with pytest.raises(ValueError, match="native EOS"):
        tokens.encode_row(row, CharacterTokenizer(add_stop=False), 256)
    with pytest.raises(ValueError, match="do not truncate"):
        tokens.encode_row(row, CharacterTokenizer(), 5)
    with pytest.raises(ValueError, match="verified pixel"):
        tokens.encode_row(row | {"frames": ["frame.jpg"]}, CharacterTokenizer(), 256)
    with pytest.raises(ValueError, match="template control"):
        tokens.encode_row(
            row | {"messages": [*row["messages"][:-1], {"role": "assistant", "content": "answer<eos>"}]},
            CharacterTokenizer(),
            256,
        )


def test_actual_token_balancing_keeps_every_train_row_and_aligns_global_batch(modules):
    _, _, tokens, _ = modules
    rows = [
        ("dafeiyu_depth", {"id": 1}, 70),
        ("anime_width", {"id": 2}, 20),
        ("general_ability", {"id": 3}, 10),
    ]
    selected = tokens.balance_training(rows, seed=42, global_batch=2)
    assert selected == tokens.balance_training(rows, seed=42, global_batch=2)
    assert all(row in selected for row in rows)
    assert len(selected) % 2 == 0
    counts = tokens.token_counts(selected)
    assert 0.27 <= counts["general_ability"] / sum(counts.values()) <= 0.33


def test_legacy_rows_keep_strict_mask_without_claiming_clean_categories(modules):
    compiler, _, tokens, _ = modules
    row = compiler.sample("source", "family", "dafeiyu_core", [("Q", "A")], system="SYS")
    legacy = {"messages": row["messages"], "category": "chit_chat_and_food"}
    with pytest.raises(ValueError, match="Unknown OC training category"):
        tokens.tokenize_split([legacy], CharacterTokenizer(), 256)
    encoded = tokens.tokenize_split([legacy], CharacterTokenizer(), 256, allow_legacy=True)
    assert encoded[0][0] == "legacy"
    assert encoded[0][2] == 2


def test_workstation_replay_retains_native_stop_vs_length():
    replay = importlib.import_module("tools.training.replay_dafeiyu_holdout_workstation")

    class Decoder:
        def decode(self, tokens, **kwargs):
            return "".join(map(chr, tokens))

    assert replay.reply_from_tokens(Decoder(), [65, 66, 99, 0], {99}) == {"text": "AB", "finish_reason": "stop"}
    assert replay.reply_from_tokens(Decoder(), [65, 66], {99}) == {"text": "AB", "finish_reason": "length"}


def test_eval_only_queue_never_compiles_or_erases_artifacts(modules, monkeypatch, tmp_path):
    _, _, _, queue = modules
    output = tmp_path / "output_queue"
    adapter = output / "adapter_qwen35_9b"
    checkpoint = adapter / "checkpoint-1"
    checkpoint.mkdir(parents=True)
    (adapter / "adapter_model.safetensors").write_bytes(b"original")
    (checkpoint / "test").write_bytes(b"keep")
    monkeypatch.setattr(
        sys, "argv", ["queue", "--repo_root", str(tmp_path), "--only_model", "qwen35_9b", "--skip_train"]
    )
    monkeypatch.setattr(
        queue, "run_command", lambda *args, **kwargs: pytest.fail("eval-only may not compile")
    )

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(2, "mock")

    monkeypatch.setattr(queue, "evaluate_model", fail)
    results = []
    monkeypatch.setattr(
        queue, "update_markdown_report", lambda rows, paths: results.append(json.loads(json.dumps(rows)))
    )
    queue.main()
    assert (checkpoint / "test").read_bytes() == b"keep"
    assert (adapter / "adapter_model.safetensors").read_bytes() == b"original"
    assert not (output / "compiled_data").exists()
    assert not next(row for row in results[-1] if row["id"] == "qwen35_9b")["evaluated"]


def test_queue_uses_current_training_entry_points(modules, monkeypatch, tmp_path):
    _, _, _, queue = modules
    training = tmp_path / "tools" / "training"
    training.mkdir(parents=True)
    for name in ("build_authentic_sft_dataset.py", "train_dora.py"):
        (training / name).touch()
    monkeypatch.setattr(
        sys, "argv", ["queue", "--repo_root", str(tmp_path), "--only_model", "qwen35_9b"]
    )
    monkeypatch.setattr(queue, "disk_has_room", lambda _: True)
    commands = []

    def capture(command, **kwargs):
        commands.append(Path(command[1]))
        return 0

    monkeypatch.setattr(queue, "run_command", capture)
    queue.main()
    assert commands == [training / "build_authentic_sft_dataset.py", training / "train_dora.py"]
