import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy", reason="Experimental Genie CPU graph tests need isolated NumPy tools")
onnx = pytest.importorskip("onnx", reason="Experimental Genie CPU graph tests need isolated ONNX tools")
from onnx.reference import ReferenceEvaluator  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "genie_sampling", Path(__file__).parents[1] / "tools/jetson/tts/genie_sampling.py"
)
sampling = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sampling)


def sampler_tail(kind):
    helper, tensor = onnx.helper, onnx.TensorProto

    def constant(name, value, dtype):
        return helper.make_node(
            "Constant",
            [],
            [name],
            name=name,
            value=onnx.numpy_helper.from_array(np.asarray(value, dtype=dtype)),
        )

    temp = "/Constant_11_output_0" if kind == "first" else "/Constant_15_output_0"
    nodes = [
        helper.make_node("Identity", ["logits"], ["/ScatterElements_output_0"], name="/ScatterElements"),
        constant(temp, 1, np.float32),
        helper.make_node("Div", ["/ScatterElements_output_0", temp], ["scaled"], name="/Div_1"),
        constant("k", 15, np.int64),
        constant("k_shape", [1], np.int64),
        helper.make_node("Reshape", ["k", "k_shape"], ["k_vector"], name="reshape_k"),
        helper.make_node("TopK", ["scaled", "k_vector"], ["top_values", "top_indices"], name="/TopK"),
        helper.make_node("ReduceMin", ["top_values"], ["threshold"], keepdims=0),
        helper.make_node("Less", ["scaled", "threshold"], ["excluded"]),
        constant("negative_inf", -np.inf, np.float32),
        helper.make_node("Where", ["excluded", "negative_inf", "scaled"], ["filtered"]),
        helper.make_node("Softmax", ["filtered"], ["/Softmax_output_0"], axis=-1),
        helper.make_node(
            "RandomNormalLike", ["/Softmax_output_0"], ["noise"], name="/RandomNormalLike", dtype=1
        ),
        helper.make_node("Div", ["/Softmax_output_0", "noise"], ["scores"]),
        helper.make_node("ArgMax", ["scores"], ["token"], axis=-1, keepdims=1),
    ]
    inputs = [helper.make_tensor_value_info("logits", tensor.FLOAT, [1025])]
    outputs = [helper.make_tensor_value_info("token", tensor.INT64, [1])]
    if kind == "stage":
        nodes += [
            helper.make_node("Identity", ["raw"], ["/ar_predict_layer/MatMul_output_0"]),
            helper.make_node(
                "ArgMax",
                ["/ar_predict_layer/MatMul_output_0"],
                ["greedy"],
                name="/ArgMax_1",
                axis=-1,
                keepdims=0,
            ),
        ]
        inputs.append(helper.make_tensor_value_info("raw", tensor.FLOAT, [1, 1025]))
        outputs.append(helper.make_tensor_value_info("greedy", tensor.INT64, [1]))
    return helper.make_model(
        helper.make_graph(nodes, "sampler_only", inputs, outputs), opset_imports=[helper.make_opsetid("", 20)]
    )


def test_first_sampling_patch_uses_top5_and_suppresses_eos_before_sampling():
    model = sampler_tail("first")
    sampling._rewrite_graph(model, "first")
    onnx.checker.check_model(model)
    evaluator = ReferenceEvaluator(model)
    logits = np.arange(1025, dtype=np.float32)
    logits[1024] = 100000
    result = evaluator.run(None, {"logits": logits, sampling.EXP_INPUT: np.ones(1025, np.float32)})
    assert int(result[0][0]) == 1023
    k = next(node for node in model.graph.node if node.name == "k")
    assert int(onnx.numpy_helper.to_array(k.attribute[0].t)) == 5
    assert not any(node.op_type.startswith("Random") for node in model.graph.node)


def test_greedy_eos_uses_penalty_adjusted_logits_not_raw_projection():
    model = sampler_tail("stage")
    sampling._rewrite_graph(model, "stage")
    onnx.checker.check_model(model)
    raw = np.zeros((1, 1025), np.float32)
    raw[0, 3] = 3
    adjusted = np.zeros(1025, np.float32)
    adjusted[1024] = 4
    result = ReferenceEvaluator(model).run(
        None, {"logits": adjusted, "raw": raw, sampling.EXP_INPUT: np.ones(1025, np.float32)}
    )
    assert int(result[1][0]) == 1024


def test_public_patch_rejects_non_pinned_graph_without_creating_output(tmp_path):
    source, output = tmp_path / "other.onnx", tmp_path / "patched.onnx"
    onnx.save(sampler_tail("first"), source)
    with pytest.raises(ValueError, match="Unsupported graph"):
        sampling.patch_graph(source, output, "first")
    assert not output.exists()


def test_graph_identity_allows_weight_relinking_but_not_node_changes():
    model = sampler_tail("first")
    tensor = onnx.helper.make_tensor("weight", onnx.TensorProto.FLOAT, [1], [1])
    tensor.ClearField("float_data")
    tensor.data_location = onnx.TensorProto.EXTERNAL
    tensor.external_data.add(key="location", value="original.bin")
    model.graph.initializer.append(tensor)
    original = sampling._fingerprint(model)
    model.graph.initializer[0].external_data[0].value = "converted.bin"
    assert sampling._fingerprint(model) == original
    model.graph.node[0].op_type = "Neg"
    assert sampling._fingerprint(model) != original


@pytest.mark.parametrize("location,size", [("../foreign.bin", 4), ("weight.bin", 1)])
def test_external_data_cannot_escape_bundle_or_point_to_short_weights(tmp_path, location, size):
    model = sampler_tail("first")
    tensor = onnx.helper.make_tensor("weight", onnx.TensorProto.FLOAT, [1], [1])
    tensor.ClearField("float_data")
    tensor.data_location = onnx.TensorProto.EXTERNAL
    for key, value in (("location", location), ("offset", "0"), ("length", "4")):
        tensor.external_data.add(key=key, value=value)
    model.graph.initializer.append(tensor)
    (tmp_path / "weight.bin").write_bytes(b"x" * size)
    with pytest.raises(ValueError):
        sampling._external_files(model, tmp_path / "source.onnx", tmp_path / "output.onnx")


class Echo:
    def __init__(self, noise_name):
        self.noise_name = noise_name

    def get_inputs(self):
        return [SimpleNamespace(name=self.noise_name)]

    def run(self, names, feed):
        return feed[self.noise_name]


def request(seed):
    return sampling.SamplingSessions(
        Echo(sampling.EXP_INPUT), Echo(sampling.EXP_INPUT), Echo(sampling.NORMAL_INPUT), seed=seed
    )


def test_seed_resets_per_request_and_segments_have_separate_positive_ar_and_normal_vits_streams():
    left, right = request(42).segment(0), request(42).segment(0)
    a, b = left.first.run(None, {}), right.first.run(None, {})
    assert np.array_equal(a, b) and a.dtype == np.float32 and (a > 0).all()
    assert np.array_equal(left.stage.run(None, {}), right.stage.run(None, {}))
    assert not np.array_equal(a, request(42).segment(1).first.run(None, {}))
    feed = {"pred_semantic": np.array([[[1, 2, 3]]], np.int64)}
    normal = left.vocoder.run(None, feed)
    assert np.array_equal(normal, right.vocoder.run(None, feed))
    assert normal.shape == (1, 192, 6) and normal.dtype == np.float32
    assert (normal < 0).any() and (normal > 0).any()


class Encoder:
    def run(self, names, feed):
        return None, np.array([[7, 8]], np.int64)


class First:
    def __init__(self, token=3):
        self.token = token

    def run(self, names, feed):
        return np.concatenate([feed["prompts"], [[self.token]]], axis=1), None


class Stage:
    def __init__(self, stop_at=None, sampled_eos=True, cancel=None):
        self.calls, self.stop_at, self.sampled_eos, self.cancel = 0, stop_at, sampled_eos, cancel

    def get_inputs(self):
        return [SimpleNamespace(name=name) for name in ("iy", "iy_emb")]

    def run(self, names, feed):
        self.calls += 1
        stop = self.calls == self.stop_at
        token = 1024 if stop and self.sampled_eos else 4
        if self.cancel:
            self.cancel.set()
        return np.concatenate([feed["iy"], [[token]]], axis=1), None, np.array(stop, dtype=np.bool_)


def decode(stage, *, first=None, event=None):
    return sampling.decode_segment(
        Encoder(),
        first or First(),
        stage,
        ref_seq=None,
        ref_bert=None,
        text_seq=np.array([[1, 2]], np.int64),
        text_bert=None,
        ssl_content=None,
        segment_index=6,
        stop_event=event,
    )


@pytest.mark.parametrize("sampled_eos", [True, False])
def test_first_stage_iteration_eos_uses_real_prefix_and_never_returns_prompt_or_artificial_zero(sampled_eos):
    semantic, info = decode(Stage(stop_at=1, sampled_eos=sampled_eos))
    assert semantic.tolist() == [[[3]]]
    assert info["complete"] and info["stage_steps"] == 1 and info["segment_index"] == 6
    assert info["stop_reason"] == ("sampled_eos" if sampled_eos else "greedy_eos")


def test_full_limit_without_eos_is_failure_but_eos_on_last_iteration_is_complete():
    with pytest.raises(sampling.IncompleteSynthesis) as failed:
        decode(Stage())
    assert failed.value.diagnostics["limit_exhausted"]
    assert failed.value.diagnostics["stage_steps"] == 500
    semantic, info = decode(Stage(stop_at=500))
    assert info["complete"] and not info["limit_exhausted"]
    assert semantic.shape == (1, 1, 500)


def test_first_eos_and_cancellation_never_return_partial_semantics():
    with pytest.raises(sampling.IncompleteSynthesis, match="invalid_first"):
        decode(Stage(), first=First(1024))
    event = threading.Event()
    with pytest.raises(sampling.IncompleteSynthesis, match="cancelled") as failed:
        decode(Stage(cancel=event), event=event)
    assert failed.value.diagnostics["stage_steps"] == 1
    assert not failed.value.diagnostics["complete"]


def test_empty_reference_prefix_is_not_complete_semantics():
    class EmptyReference:
        def run(self, names, feed):
            return None, np.empty((1, 0), np.int64)

    with pytest.raises(sampling.IncompleteSynthesis, match="invalid_reference_shape"):
        sampling.decode_segment(
            EmptyReference(),
            First(),
            Stage(),
            ref_seq=None,
            ref_bert=None,
            text_seq=np.array([[1]], np.int64),
            text_bert=None,
            ssl_content=None,
        )
