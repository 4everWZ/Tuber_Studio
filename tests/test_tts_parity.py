import array
import importlib.util
import wave
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "tts_parity", Path(__file__).parents[1] / "tools/evaluate_tts_parity.py"
)
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


def wav(path, samples, *, rate=32000):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(array.array("h", samples).tobytes())
    return path


def test_pcm_difference_does_not_overflow_int16(tmp_path):
    left = wav(tmp_path / "left.wav", [-32768, 2])
    right = wav(tmp_path / "right.wav", [32767, 1])
    row = parity.difference(left, right)
    assert row["comparison_available"]
    assert not row["bit_identical"]
    assert row["max_abs_pcm16"] == 65535
    assert row["changed_samples"] == 2
    assert row["mse_pcm16"] == (65535**2 + 1) / 2


def test_shape_mismatch_has_no_sample_aligned_metrics(tmp_path):
    left = wav(tmp_path / "left.wav", [1, 2])
    right = wav(tmp_path / "right.wav", [1, 2, 3])
    row = parity.difference(left, right)
    assert not row["comparison_available"]
    assert not row["bit_identical"]
    assert row["max_abs_pcm16"] is None
    assert row["mse_pcm16"] is None
    assert "shapes differ" in row["unavailable_reason"]


@pytest.mark.parametrize("invalid", ["truncated", "silent"])
def test_invalid_wav_cannot_supply_parity_evidence(tmp_path, invalid):
    path = wav(tmp_path / "invalid.wav", [0, 0] if invalid == "silent" else [1, 2])
    if invalid == "truncated":
        path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError):
        parity.inspect_wav(path)
    row = parity.compare_files(path, path, kind="same_order", index=0, repeat=0)
    assert not row["comparison_available"]
    assert not row["bit_identical"]
    assert row["unavailable_reason"]


def pairs():
    return [
        {
            "kind": "same_order",
            "index": index,
            "repeat": repeat,
            "comparison_available": True,
            "bit_identical": True,
        }
        for index in range(3)
        for repeat in range(2)
    ]


@pytest.mark.parametrize("problem", ["missing", "duplicate", "failed_variant", "shape"])
def test_six_unique_available_pairs_and_completed_variants_are_required(problem):
    rows = pairs()
    completed = True
    if problem == "missing":
        rows.pop()
    elif problem == "duplicate":
        rows[-1] = rows[0].copy()
    elif problem == "failed_variant":
        completed = False
    else:
        rows[0]["comparison_available"] = False
    summary = parity.same_order_summary(rows, variants_complete=completed)
    assert not summary["bitwise_passed"]


def test_legacy_and_self_repeat_differences_are_separate_from_matched_pair_gate():
    rows = pairs() + [
        {"kind": "legacy", "bit_identical": False},
        {"kind": "self_repeat", "bit_identical": False},
    ]
    summary = parity.same_order_summary(rows, variants_complete=True)
    assert summary["bitwise_passed"]
    assert summary["reported_pairs"] == 6
    assert not summary["missing_pairs"]


def test_warmup_tracks_production_literal_and_rejects_ambiguous_changes(tmp_path):
    server = tmp_path / "tools/jetson/tts/server.py"
    server.parent.mkdir(parents=True)
    source = "class SpeechEngine:\n def load(self):\n  engine.request({'text':'暖机', 'seed':42})\n"
    server.write_text(source, encoding="utf-8")
    assert parity.production_warmup(tmp_path) == {"text": "暖机", "seed": 42}
    server.write_text(source + "  engine.request({'text':'多一次', 'seed':42})\n", encoding="utf-8")
    with pytest.raises(ValueError, match="contract changed"):
        parity.production_warmup(tmp_path)
