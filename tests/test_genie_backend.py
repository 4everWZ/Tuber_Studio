"""CPU-only service contract checks; no Torch, ORT execution or model loading."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy", reason="Genie runtime CPU contracts require NumPy")


@pytest.fixture
def backend(monkeypatch):
    compact = SimpleNamespace(reference_signature=lambda *a, **k: {"voice": {"id": "oc"}})
    monkeypatch.setitem(sys.modules, "compact", compact)
    path = Path(__file__).parents[1] / "tools/jetson/tts/genie_backend.py"
    spec = importlib.util.spec_from_file_location("genie_backend_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "params",
    [
        {"top_k": 15},
        {"top_p": 0.9},
        {"temperature": 0.8},
        {"repetition_penalty": 1},
        {"text_split_method": "cut0"},
        {"speed_factor": 1.1},
        {"batch_size": 2},
        {"parallel_infer": False},
        {"split_bucket": 1},
        {"text_lang": "en"},
        {"batch_threshold": 0.5},
        {"seed": True},
        {"seed": -2},
        {"seed": 2**32},
        {"fragment_interval": float("nan")},
        {"fragment_interval": True},
        {"fragment_interval": 400},
    ],
)
def test_unsupported_controls_are_rejected_before_any_model_work(backend, params):
    with pytest.raises(ValueError):
        backend.validate_params(params)


def test_original_defaults_seed_range_and_variable_gap_are_supported(backend):
    for seed in (-1, 0, 42, 2**32 - 1):
        backend.validate_params({"seed": seed, "fragment_interval": 0.05})
    backend.validate_params({})


def test_postprocess_normalizes_each_fragment_and_pads_final_fragment(backend):
    first = np.array([0.25, -0.25], dtype=np.float32)
    second = np.array([2.0, -2.0, 0.5], dtype=np.float32)
    pcm = backend.postprocess([first, second], fragment_interval=0.01)
    assert pcm.dtype == np.int16 and pcm.shape == (645,)
    assert pcm[:2].tolist() == [8192, -8192]
    assert not pcm[2:322].any()
    # Upstream casts rather than clips: +32768 also wraps to -32768.
    assert pcm[322:325].tolist() == [-32768, -32768, 8192]
    assert not pcm[325:].any()
    assert second.tolist() == [2.0, -2.0, 0.5]
    assert backend.postprocess([first], fragment_interval=0).size == 322


@pytest.mark.parametrize("audio", [np.array([]), np.array([np.nan]), np.zeros((1, 2))])
def test_invalid_vocoder_audio_does_not_become_success(backend, audio):
    with pytest.raises(RuntimeError):
        backend.postprocess([audio])


def test_audio_budget_is_checked_before_gap_allocation(backend, monkeypatch):
    monkeypatch.setattr(backend, "MAX_SAMPLES", 100)
    with pytest.raises(RuntimeError, match="budget"):
        backend.postprocess([np.ones(2, dtype=np.float32)])


@pytest.fixture
def pipeline(backend, tmp_path, monkeypatch):
    calls, changed = [], []
    arrays = {
        "prompt_semantic": np.array([7, 8], dtype=np.int64),
        "reference_phones": np.array([3, 4], dtype=np.int64),
        "reference_bert": np.ones((1024, 2), dtype=np.float16),
    }

    class Feature:
        @property
        def T(self):
            return self

        def contiguous(self):
            return self

        def numpy(self):
            return np.ones((2, 1024), dtype=np.float32)

    class Vocoder:
        def run(self, names, feed):
            calls.append(("vocoder", feed))
            return [np.array([[[0.25, -0.25]]], dtype=np.float32)]

    class Encoder:
        def run(self, names, feed):
            assert set(feed) == {"ref_seq", "ref_bert", "text_seq", "text_bert"}
            return [np.ones((1, 4), dtype=np.float32)]

    class Incomplete(RuntimeError):
        def __init__(self):
            self.diagnostics = {"complete": False, "stop_reason": "step_limit_without_eos"}

    class Sampler:
        def __init__(self, first, stage, vocoder, *, seed):
            calls.append(("seed", seed))
            self.entropy, self.vocoder = 123, vocoder

        def segment(self, index):
            calls.append(("segment", index))
            return SimpleNamespace(first=None, stage=None, vocoder=self.vocoder)

    def decode(encoder, first, stage, **kwargs):
        encoder.run(
            None,
            {key: kwargs[key] for key in ("ref_seq", "ref_bert", "text_seq", "text_bert", "ssl_content")},
        )
        calls.append(("decode", kwargs))
        return np.array([[[2, 3]]], dtype=np.int64), {"complete": True, "semantic_tokens": 2}

    def unchanged(signature):
        if changed:
            raise ValueError("Bundle changed")

    sampler = SimpleNamespace(SamplingSessions=Sampler, decode_segment=decode, IncompleteSynthesis=Incomplete)
    helpers = {"genie_sampling": sampler, "genie_bundle": SimpleNamespace(verify_unchanged=unchanged)}
    monkeypatch.setattr(backend, "_sibling", lambda name: helpers[name])
    frontend = SimpleNamespace(
        preprocess=lambda *args: (
            [{"phones": [1, 2], "bert_features": Feature(), "norm_text": "规范中文。"}] * 2
        )
    )
    instance = backend.GeniePipeline(
        tmp_path,
        "oc",
        {
            "identity": "a" * 64,
            "stat_signature": {"original": True},
            "reference_metadata": {"identity": {}},
            "reference_arrays": arrays,
        },
        frontend=frontend,
        encoder=Encoder(),
        first=None,
        stage=None,
        vocoder=Vocoder(),
        reference=np.ones(5, dtype=np.float32),
        held={},
    )
    return instance, calls, changed, sampler


def test_runtime_uses_cached_reference_strict_decoder_and_original_output_postprocess(pipeline):
    instance, calls, _, _ = pipeline
    result = list(instance.run({"text": "新的动态中文。", "seed": 42}))
    assert len(result) == 1 and result[0][0] == 32000
    assert result[0][1].dtype == np.int16 and result[0][1].size == 19204
    assert [entry for entry in calls if entry[0] == "segment"] == [("segment", 0), ("segment", 1)]
    assert ("seed", 42) in calls
    assert instance.last_diagnostics["complete"] is True
    assert instance.last_diagnostics["raw_frames"] == 4
    assert instance.last_diagnostics["output_frames"] == 19204


def test_bundle_change_and_incomplete_decode_never_yield_wav(pipeline):
    instance, calls, changed, sampler = pipeline
    changed.append(True)
    with pytest.raises(ValueError, match="changed"):
        list(instance.run({"text": "你好"}))
    assert calls == []
    changed.clear()

    def fail(*args, **kwargs):
        raise sampler.IncompleteSynthesis()

    sampler.decode_segment = fail
    with pytest.raises(sampler.IncompleteSynthesis):
        list(instance.run({"text": "你好"}))
    assert instance.last_diagnostics["complete"] is False
    assert instance.last_diagnostics["segments"] == [
        {"complete": False, "stop_reason": "step_limit_without_eos"}
    ]
    assert not any(name == "vocoder" for name, _ in calls)


def test_only_fixed_sibling_helpers_can_be_imported(backend):
    with pytest.raises(ValueError, match="Unknown fixed"):
        backend._sibling("../../runtime/untrusted")


def frontend_item(text, *, phones=None, values=None):
    class Feature:
        @property
        def T(self):
            return self

        def contiguous(self):
            return self

        def numpy(self):
            return np.zeros((2, 1024), dtype=np.float32) if values is None else values

    return {"norm_text": text, "phones": [1, 2] if phones is None else phones, "bert_features": Feature()}


def test_mixed_request_preserves_english_zero_features_and_dynamic_chinese_features(pipeline):
    instance, calls, _, _ = pipeline
    instance.frontend.preprocess = lambda *args: [
        frontend_item("Hello,"),
        frontend_item("欢迎来到海底厨房。", values=np.ones((2, 1024), dtype=np.float32)),
    ]
    output = list(instance.run({"text": "Hello，欢迎来到海底厨房。", "seed": 42}))
    decoded = [value for name, value in calls if name == "decode"]
    assert len(output) == 1 and len(decoded) == 2
    assert not decoded[0]["text_bert"].any() and decoded[1]["text_bert"].any()
    assert [item["text_feature_kind"] for item in instance.last_diagnostics["segments"]] == ["latin", "han"]


@pytest.mark.parametrize("text", ["中文。", "Hello中文。", "\U00020000。", "\U00031350。"])
def test_any_han_segment_cannot_fall_back_to_zero_bert(backend, text):
    with pytest.raises(RuntimeError, match="BERT"):
        backend._item_features(frontend_item(text))
    phones, bert, kind = backend._item_features(
        frontend_item(text, values=np.ones((2, 1024), dtype=np.float32))
    )
    assert phones.shape == (1, 2) and bert.shape == (2, 1024) and kind == "han"


@pytest.mark.parametrize(
    "item",
    [
        frontend_item(""),
        frontend_item("   "),
        frontend_item("!!!"),
        frontend_item("123"),
        frontend_item("Привет"),
        frontend_item("こんにちは"),
        frontend_item("Hello🙂"),
        frontend_item("Hello", phones=[]),
        frontend_item("Hello", phones=[-1, 2]),
        frontend_item("Hello", phones=[True, False]),
        frontend_item("Hello", phones=[1.5, 2.5]),
        frontend_item("Hello", values=np.zeros((1, 1024), dtype=np.float32)),
        frontend_item("Hello", values=np.full((2, 1024), np.nan, dtype=np.float32)),
    ],
)
def test_empty_unknown_script_and_invalid_phone_or_feature_results_are_rejected(backend, item):
    with pytest.raises(RuntimeError):
        backend._item_features(item)


@pytest.mark.parametrize("location", ["vits_fp32.bin", "../outside.bin"])
def test_vits_mmap_lifetime_and_external_path_guard(backend, tmp_path, location):
    onnx = pytest.importorskip("onnx")
    weight = onnx.numpy_helper.from_array(np.array([0.125, 0.25], dtype=np.float32), name="weight")
    onnx.external_data_helper.set_external_data(weight, location=location, offset=0, length=8)
    weight.ClearField("raw_data")
    graph = onnx.helper.make_graph(
        [onnx.helper.make_node("Identity", ["weight"], ["output"])],
        "tiny_external",
        [],
        [onnx.helper.make_tensor_value_info("output", onnx.TensorProto.FLOAT, [2])],
        [weight],
    )
    model = tmp_path / "vits.onnx"
    onnx.save(onnx.helper.make_model(graph), model)
    np.array([0.125, 0.25], dtype=np.float32).tofile(tmp_path / "vits_fp32.bin")
    original_files = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    class Options:
        def __init__(self):
            self.initializers, self.config = {}, {}

        def add_session_config_entry(self, key, value):
            self.config[key] = value

        def add_initializer(self, name, value):
            self.initializers[name] = value

    ort = SimpleNamespace(
        SessionOptions=Options,
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=99),
        OrtValue=SimpleNamespace(ortvalue_from_numpy=lambda value: value),
        InferenceSession=lambda path, **kwargs: SimpleNamespace(**kwargs),
    )
    held = {}
    if location != "vits_fp32.bin":
        with pytest.raises(ValueError, match="checked adjacent"):
            backend._session(model, ort, held=held)
        assert held == {}
    else:
        session = backend._session(model, ort, held=held)
        assert session.providers == ["CPUExecutionProvider"]
        assert session.sess_options.intra_op_num_threads == 6
        assert session.sess_options.inter_op_num_threads == 1
        assert session.sess_options.config["session.use_env_allocators"] == "1"
        assert np.shares_memory(held["array"], held["values"][0])
        assert not held["array"].flags.writeable
        assert held["values"][0].tolist() == [0.125, 0.25]
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original_files
