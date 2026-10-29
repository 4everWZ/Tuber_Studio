"""Experimental pinned Genie v2 sampling restoration; not the production backend.

Restores GPT-SoVITS Exp(1)/top-k5 and first-step EOS suppression. VITS keeps
standard-normal noise and its existing 0.5 scale. Caller stages a converted
FP32 bundle in a NEW experiment directory before patching to a new filename.
Request-local NumPy streams are reproducible within a fixed environment, not
Torch's random sequence or a promise of cross-backend bit-identical audio.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path
from types import SimpleNamespace

REVISION = "d347fd0f8683e9a362b69f59fa0a4799ddb5e828"
PINNED = {
    "first": {
        "sha256": "ee7ddd22f27247f49b028c5ae8911f1f8b9ce97538c612dbb9f96057799da4c2",
        "graph": "a473cbe5a4a6c2d7d485931c9dcd60eb077285cbee005542ecc7fa7bc9df317f",
        "nodes": 1821,
    },
    "stage": {
        "sha256": "de6189e17efbad95479be95ca3691f19e7c69bdaaf63b171aa27283e41660dea",
        "graph": "3077a8727aac8af6365ba914b50590b10cd3dd9a31eee6f787ab1439def1d119",
        "nodes": 1822,
    },
    "vits": {
        "sha256": "634c21025f9fea03901c1fb5741a98ada512adeb67183e79b261c059fa4d842a",
        "graph": "c0bade3b478d6d30b2e28c244eea18986daf1bdf3624bdb88456264cdb0f1b79",
        "nodes": 8455,
    },
}
EXP_INPUT = "tuber_exp_noise"
NORMAL_INPUT = "tuber_normal_noise"


def _fingerprint(model):
    """Exact nodes/IO/opsets and initializer identities; allow only relinking weights."""
    digest = hashlib.sha256()
    for value in (
        [model.ir_version],
        [(item.domain, item.version) for item in model.opset_import],
        [(item.name, item.data_type, list(item.dims)) for item in model.graph.initializer],
    ):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        digest.update(struct.pack("<Q", len(raw)))
        digest.update(raw)
    for item in [*model.graph.node, *model.graph.input, *model.graph.output]:
        raw = item.SerializeToString(deterministic=True)
        digest.update(struct.pack("<Q", len(raw)))
        digest.update(raw)
    return digest.hexdigest()


def _external_files(model, source, output):
    """Keep legal external-data names; never invent ../ links or copy large bins."""
    files, verified = set(), set()
    for tensor in model.graph.initializer:
        if tensor.data_location != 1:
            raise ValueError("Expected external FP32 weight initializers")
        entries = {item.key: item.value for item in tensor.external_data}
        location = entries.get("location", "")
        if not location or any(char in location for char in "/\\:") or location in {".", ".."}:
            raise ValueError("External data must use a safe local basename")
        offset, length = int(entries["offset"]), int(entries["length"])
        if offset < 0 or length != math.prod(tensor.dims) * 4 or tensor.data_type != 1:
            raise ValueError("Invalid external FP32 weight layout")
        original, staged = source.parent / location, output.parent / location
        if original.is_symlink() or staged.is_symlink() or not original.is_file() or not staged.is_file():
            raise ValueError("Stage the original converted FP32 bins alongside the new graph first")
        if original.stat().st_size < offset + length or staged.stat().st_size != original.stat().st_size:
            raise ValueError("Truncated or mismatched staged external data")
        pair = (original.resolve(), staged.resolve())
        if pair[0] != pair[1] and pair not in verified:
            # Different directories require byte-identical staged copies. This
            # preparation cost is outside timed model loading, never a hidden copy.
            for path in (original, staged):
                with path.open("rb") as stream:
                    value = hashlib.file_digest(stream, "sha256").hexdigest()
                if path == original:
                    expected = value
                elif value != expected:
                    raise ValueError("Staged external data differs from the source bundle")
            verified.add(pair)
        files.add(location)
    return sorted(files)


def _rewrite_graph(model, kind):
    import numpy as np
    import onnx

    nodes = {node.name: node for node in model.graph.node}
    random_name = "/vq_model/RandomNormalLike" if kind == "vits" else "/RandomNormalLike"
    random = nodes[random_name]
    expected_input = "/vq_model/enc_p/Split_output_0" if kind == "vits" else "/Softmax_output_0"
    if (
        random.op_type != "RandomNormalLike"
        or random.domain
        or list(random.input) != [expected_input]
        or [(a.name, onnx.helper.get_attribute_value(a)) for a in random.attribute] != [("dtype", 1)]
    ):
        raise ValueError("Pinned random-node producer changed")
    name = NORMAL_INPUT if kind == "vits" else EXP_INPUT
    shape = [1, 192, "tuber_frames"] if kind == "vits" else [1025]
    random.op_type, random.name = "Identity", "tuber_external_noise"
    del random.input[:]
    random.input.append(name)
    del random.attribute[:]
    model.graph.input.append(onnx.helper.make_tensor_value_info(name, onnx.TensorProto.FLOAT, shape))
    if kind == "vits":
        return
    producers = {value: node for node in model.graph.node for value in node.output}
    topk = nodes["/TopK"]
    reshape = producers[topk.input[1]]
    constant = producers[reshape.input[0]]
    if reshape.op_type != "Reshape" or constant.op_type != "Constant":
        raise ValueError("Pinned TopK producer changed")
    tensor = next(a.t for a in constant.attribute if a.name == "value")
    if int(onnx.numpy_helper.to_array(tensor)) != 15:
        raise ValueError("Pinned TopK is not 15")
    tensor.CopyFrom(onnx.numpy_helper.from_array(np.array(5, dtype=np.int64)))
    if kind == "first":
        divide = nodes["/Div_1"]
        if list(divide.input) != ["/ScatterElements_output_0", "/Constant_11_output_0"]:
            raise ValueError("Pinned first-step logits producer changed")
        mask = [
            onnx.helper.make_node(
                "Constant",
                [],
                ["tuber_eos_index"],
                name="tuber_eos_index",
                value=onnx.numpy_helper.from_array(np.array([1024], dtype=np.int64)),
            ),
            onnx.helper.make_node(
                "Constant",
                [],
                ["tuber_eos_value"],
                name="tuber_eos_value",
                value=onnx.numpy_helper.from_array(np.array([-np.inf], dtype=np.float32)),
            ),
            onnx.helper.make_node(
                "ScatterElements",
                [divide.input[0], "tuber_eos_index", "tuber_eos_value"],
                ["tuber_first_logits"],
                name="tuber_mask_first_eos",
                axis=0,
            ),
        ]
        position = next(i for i, node in enumerate(model.graph.node) if node.name == divide.name)
        divide.input[0] = "tuber_first_logits"
        for index, node in enumerate(mask):
            model.graph.node.insert(position + index, node)
    else:
        greedy = nodes["/ArgMax_1"]
        if list(greedy.input) != ["/ar_predict_layer/MatMul_output_0"]:
            raise ValueError("Pinned greedy-EOS producer changed")
        # Original utils.scatter_ applies the repetition penalty in-place before
        # the decoder checks greedy EOS. Preserve its [1] result shape here.
        greedy.input[0] = "/ScatterElements_output_0"
        next(a for a in greedy.attribute if a.name == "keepdims").i = 1


def patch_graph(path, output, kind):
    """Write a new guarded graph; caller supplies staged, real FP32 external bins."""
    import onnx

    if kind not in PINNED:
        raise ValueError("Unknown fixed Genie graph kind")
    path, output = Path(path).resolve(), Path(output).resolve()
    if output.exists() or output == path or output.suffix != ".onnx" or not output.parent.is_dir():
        raise ValueError("Output must be a new .onnx file in an existing experiment bundle")
    if path.stat().st_size > 4_000_000:
        raise ValueError("Expected a small external-weight graph, not embedded model weights")
    model = onnx.load(path, load_external_data=False)
    if (
        model.functions
        or model.training_info
        or model.graph.sparse_initializer
        or len(model.graph.node) != PINNED[kind]["nodes"]
        or _fingerprint(model) != PINNED[kind]["graph"]
    ):
        raise ValueError("Unsupported graph; expected unchanged pinned d347 v2 template")
    external = _external_files(model, path, output)
    _rewrite_graph(model, kind)
    with output.open("xb") as stream:
        stream.write(model.SerializeToString())
    try:
        onnx.checker.check_model(str(output))
    except BaseException:
        output.unlink()
        raise
    return {
        "kind": kind,
        "revision": REVISION,
        "official_template_sha256": PINNED[kind]["sha256"],
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "output": str(output),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "external_files": external,
        "noise_input": NORMAL_INPUT if kind == "vits" else EXP_INPUT,
        "sampling": "standard normal; original 0.5 VITS scale"
        if kind == "vits"
        else "Exp(1), top_k=5, top_p=1, temperature=1, repetition_penalty=1.35",
        "first_eos_suppressed": kind == "first",
    }


class IncompleteSynthesis(RuntimeError):
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        super().__init__("Incomplete semantic decoding: " + diagnostics["stop_reason"])


class NoiseSession:
    """A single segment's session wrapper; no process-global RNG or hidden reseed."""

    def __init__(self, session, kind, rng):
        if kind not in PINNED:
            raise ValueError("Unknown fixed Genie session kind")
        self.session, self.kind, self.rng = session, kind, rng
        self.noise_name = NORMAL_INPUT if kind == "vits" else EXP_INPUT
        if self.noise_name not in {item.name for item in session.get_inputs()}:
            raise ValueError("Session is not patched for explicit noise")

    def get_inputs(self):
        return [item for item in self.session.get_inputs() if item.name != self.noise_name]

    def run(self, names, feed):
        import numpy as np

        if self.noise_name in feed:
            raise ValueError("Caller cannot override owned random inputs")
        if self.kind == "vits":
            semantic = np.asarray(feed["pred_semantic"])
            if (
                semantic.ndim != 3
                or semantic.shape[:2] != (1, 1)
                or not semantic.shape[-1]
                or not np.issubdtype(semantic.dtype, np.integer)
                or not ((semantic >= 0) & (semantic < 1024)).all()
            ):
                raise ValueError("VITS requires one nonempty semantic sequence")
            noise = self.rng.standard_normal((1, 192, 2 * semantic.shape[-1]), dtype=np.float32)
        else:
            noise = self.rng.exponential(1.0, size=(1025,)).astype(np.float32)
            if not (noise > 0).all():
                raise ValueError("Exponential noise must be strictly positive")
        if not np.isfinite(noise).all():
            raise ValueError("Random input must be finite")
        return self.session.run(names, {**feed, self.noise_name: noise})


class SamplingSessions:
    def __init__(self, first, stage, vocoder, *, seed):
        import numpy as np

        if type(seed) is not int or not -1 <= seed <= 4294967295:
            raise ValueError("Seed must be -1 or an unsigned 32-bit integer")
        self.sessions = first, stage, vocoder
        self.entropy = np.random.SeedSequence(None if seed == -1 else seed).entropy

    def segment(self, segment_index):
        import numpy as np

        if type(segment_index) is not int or segment_index < 0:
            raise ValueError("Segment index must be a nonnegative integer")
        ar = np.random.Generator(
            np.random.PCG64(np.random.SeedSequence(self.entropy, spawn_key=(segment_index, 0)))
        )
        vits = np.random.Generator(
            np.random.PCG64(np.random.SeedSequence(self.entropy, spawn_key=(segment_index, 1)))
        )
        return SimpleNamespace(
            first=NoiseSession(self.sessions[0], "first", ar),
            stage=NoiseSession(self.sessions[1], "stage", ar),
            vocoder=NoiseSession(self.sessions[2], "vits", vits),
        )


def decode_segment(
    encoder,
    first_stage_decoder,
    stage_decoder,
    *,
    ref_seq,
    ref_bert,
    text_seq,
    text_bert,
    ssl_content,
    segment_index=0,
    stop_event=None,
    max_steps=500,
):
    """Own the small AR orchestration loop, not model/attention/sampling forwards."""
    import numpy as np

    if type(max_steps) is not int or not 1 <= max_steps <= 500:
        raise ValueError("Decode limit must be 1..500")
    diagnostics = {
        "segment_index": segment_index,
        "phone_count": int(np.asarray(text_seq).size),
        "stage_steps": 0,
        "step_limit": max_steps,
        "complete": False,
    }

    def fail(reason):
        diagnostics["stop_reason"] = reason
        raise IncompleteSynthesis(dict(diagnostics))

    def cancelled():
        if stop_event is not None and stop_event.is_set():
            fail("cancelled")

    cancelled()
    x, prompts = encoder.run(
        None,
        {
            "ref_seq": ref_seq,
            "text_seq": text_seq,
            "ref_bert": ref_bert,
            "text_bert": text_bert,
            "ssl_content": ssl_content,
        },
    )
    prefix = np.asarray(prompts)
    if prefix.ndim != 2 or prefix.shape[0] != 1 or not prefix.shape[1]:
        fail("invalid_reference_shape")
    prefix_len = prefix.shape[1]
    diagnostics["prefix_tokens"] = prefix_len
    y, y_emb, *cache = first_stage_decoder.run(None, {"x": x, "prompts": prompts})
    if (
        y.shape != (1, prefix_len + 1)
        or not np.array_equal(y[:, :prefix_len], prefix)
        or not np.issubdtype(y.dtype, np.integer)
        or not 0 <= int(y[0, -1]) < 1024
    ):
        fail("invalid_first_token_or_eos")
    names = [item.name for item in stage_decoder.get_inputs()]
    for step in range(1, max_steps + 1):
        cancelled()
        values = [y, y_emb, *cache]
        if len(names) != len(values):
            fail("decoder_input_mismatch")
        previous_len = y.shape[1]
        y, y_emb, stop, *cache = stage_decoder.run(None, dict(zip(names, values, strict=True)))
        diagnostics["stage_steps"] = step
        if y.shape != (1, previous_len + 1) or not np.issubdtype(y.dtype, np.integer):
            fail("invalid_decoder_sequence")
        stop = np.asarray(stop)
        if stop.size != 1 or stop.dtype != np.bool_:
            fail("invalid_stop_flag")
        sampled_eos = int(y[0, -1]) == 1024
        if not 0 <= int(y[0, -1]) <= 1024 or (sampled_eos and not bool(stop.item())):
            fail("invalid_token_or_stop")
        if bool(stop.item()):
            semantic = y[:, prefix_len:-1]
            if not semantic.size or not ((semantic >= 0) & (semantic < 1024)).all():
                fail("empty_or_invalid_semantics")
            diagnostics.update(
                complete=True,
                stop_reason="sampled_eos" if sampled_eos else "greedy_eos",
                semantic_tokens=int(semantic.shape[1]),
                limit_exhausted=False,
            )
            return semantic[None, ...], diagnostics
    diagnostics["limit_exhausted"] = True
    fail("step_limit_without_eos")
