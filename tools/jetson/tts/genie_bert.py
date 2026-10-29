"""Offline original-checkpoint BERT INT8 preparation and a Torch-free ORT adapter.

Preparation must run alone under the controller's outer memory watch. It never
starts/stops services, downloads weights, or changes production dependencies.
Numerical hidden-state comparisons do not establish speech quality or speed.
"""

from __future__ import annotations

import argparse
import ast
import gc
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

FP32 = "bert_fp32.onnx"
INT8 = "bert_int8.onnx"
INPUTS = ("input_ids", "attention_mask", "token_type_ids")
PREFIX = "TUBER_GENIE_BERT "


def emit(stage, **fields):
    print(PREFIX + json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def checked_path(path, boundary, *, exists=True):
    """Refuse lexical escapes and symlinks before resolving any existing path."""
    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    if not path.is_relative_to(boundary):
        raise ValueError("Path escaped its permitted project directory")
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("Symbolic links and junctions are not permitted")
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(boundary.resolve(strict=True)):
        raise ValueError("Resolved path escaped its permitted project directory")
    return resolved


def load_bert_session(bundle, options=None):
    """Load only the offline INT8 graph; options is an ORT SessionOptions object.

    The caller retains its local original tokenizer and original Chinese
    normalization/word2ph expansion. This function does not import Torch,
    Transformers, or construct a model from the source checkpoint.
    """
    import onnxruntime as ort

    directory = Path(bundle).absolute()
    checked_path(directory, directory)
    receipt_path = checked_path(directory / "receipt.json", directory)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        receipt.get("kind") != "original_chinese_bert_22_layer_dynamic_int8"
        or receipt.get("completed") is not True
    ):
        raise ValueError("BERT bundle has no successful original-checkpoint preparation receipt")
    path = checked_path(directory / INT8, directory)
    # The quantizer uses this fixed, adjacent external-data file when needed.
    external = directory / (INT8 + ".data")
    if external.exists() or external.is_symlink():
        checked_path(external, directory)
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


def feature(session, tokenized):
    """Run this utterance's tokens and return finite float32 [1,T,1024]."""
    import numpy as np

    feed = {
        item.name: np.ascontiguousarray(tokenized[item.name], dtype=np.int64) for item in session.get_inputs()
    }
    ids = feed["input_ids"]
    if ids.ndim != 2 or ids.shape[0] != 1 or ids.shape[1] < 2:
        raise ValueError("BERT requires one tokenized utterance including special tokens")
    if any(value.shape != ids.shape for value in feed.values()):
        raise ValueError("BERT token inputs must share [1,T]")
    hidden = session.run(["last_hidden_state"], feed)[0]
    if hidden.shape != (1, ids.shape[1], 1024) or hidden.dtype != np.float32 or not np.isfinite(hidden).all():
        raise RuntimeError("Invalid original BERT hidden-state output")
    return hidden


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def release_memory():
    gc.collect()
    if os.name == "posix":
        import ctypes

        try:
            ctypes.CDLL(None).malloc_trim(0)
        except AttributeError:
            pass


def speech_texts(root):
    source = checked_path(root / "tools/evaluate_tts_memory.py", root)
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(n, ast.Name) and n.id == "TEXTS" for n in node.targets
        ):
            texts = ast.literal_eval(node.value)
            if len(texts) == 3 and all(isinstance(value, str) and value for value in texts):
                return list(texts)
    raise ValueError("The existing three canonical speech texts were not found")


def prepare(args, root, base, output, receipt):
    import numpy as np
    import onnx
    import onnxruntime as ort
    import torch
    import transformers
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    if ort.__version__ != "1.22.1":
        raise RuntimeError("Preparation requires the reviewed isolated ORT1.22.1 environment")
    receipt["versions"] = {
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "onnx": onnx.__version__,
        "onnxruntime": ort.__version__,
    }
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)
    started = time.monotonic()
    emit("loading_original_checkpoint")
    masked = AutoModelForMaskedLM.from_pretrained(
        base,
        torch_dtype=torch.float32,
        local_files_only=True,
        trust_remote_code=False,
        attn_implementation="eager",
    )
    original_layers = len(masked.bert.encoder.layer)
    if (masked.config.model_type, original_layers, masked.config.hidden_size) != ("bert", 24, 1024):
        raise ValueError("Requires the original 24-layer, width-1024 Chinese BERT checkpoint")
    backbone = masked.bert
    backbone.encoder.layer = torch.nn.ModuleList(list(backbone.encoder.layer)[:22])
    backbone.config.num_hidden_layers = 22
    del masked
    backbone.eval()
    receipt["original_layers"] = original_layers
    receipt["effective_layers"] = len(backbone.encoder.layer)
    receipt["source_hidden_state"] = "original hidden_states[-3], i.e. layer 22 output"
    receipt["load_seconds"] = time.monotonic() - started

    class Hidden(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                output_hidden_states=False,
                return_dict=False,
            )[0]

    model = Hidden(backbone).eval()
    texts = speech_texts(root)
    # Additional lengths exercise symbolic sequence length, not cached frontend
    # features: the runtime adapter always receives freshly tokenized text.
    cases = texts + ["你好。", texts[0] + texts[1] + texts[2], texts[1] * 4]
    examples = []
    warmup = tokenizer(texts[0], return_tensors="pt")
    with torch.inference_mode():
        model(*(warmup[name] for name in INPUTS))
        for index, text in enumerate(cases):
            tokens = tokenizer(text, return_tensors="pt", truncation=False)
            if tokens["input_ids"].shape[1] > backbone.config.max_position_embeddings:
                raise ValueError("Feature comparison exceeded the original position limit")
            hot_started = time.monotonic()
            expected = model(*(tokens[name] for name in INPUTS)).numpy().copy()
            examples.append(
                {
                    "index": index,
                    "canonical_speech": index < 3,
                    "text": text,
                    "tokens": {name: tokens[name].numpy().copy() for name in INPUTS},
                    "expected": expected,
                    "torch_hot_seconds": time.monotonic() - hot_started,
                }
            )
    raw = output / "work/export.onnx"
    started = time.monotonic()
    emit("exporting", effective_layers=22, opset=20, dynamo=False)
    with torch.inference_mode():
        torch.onnx.export(
            model,
            tuple(warmup[name] for name in INPUTS),
            str(raw),
            input_names=list(INPUTS),
            output_names=["last_hidden_state"],
            dynamic_axes={name: {1: "tokens"} for name in (*INPUTS, "last_hidden_state")},
            opset_version=20,
            dynamo=False,
            do_constant_folding=True,
            export_params=True,
            external_data=True,
        )
    receipt["export_seconds"] = time.monotonic() - started
    del model, backbone, warmup, tokens
    release_memory()

    # Legacy export may embed sub-2GiB weights despite external_data=True.
    # Store the published FP32 model with one explicit adjacent external file.
    started = time.monotonic()
    exported = onnx.load(raw, load_external_data=True)
    fp32 = output / FP32
    onnx.save_model(
        exported,
        fp32,
        save_as_external_data=True,
        all_tensors_to_one_file=True,
        location=FP32 + ".data",
        size_threshold=1024,
    )
    del exported
    raw.unlink()
    release_memory()
    onnx.checker.check_model(str(fp32))
    receipt["externalize_seconds"] = time.monotonic() - started
    metadata = onnx.load(fp32, load_external_data=False)
    initializers = {value.name: value for value in metadata.graph.initializer}
    selected = [
        node.name
        for node in metadata.graph.node
        if node.op_type == "MatMul"
        and len(node.input) == 2
        and node.input[1] in initializers
        and initializers[node.input[1]].data_type == onnx.TensorProto.FLOAT
        and len(initializers[node.input[1]].dims) == 2
    ]
    if not selected or not all(selected) or len(selected) != len(set(selected)):
        raise ValueError("Expected uniquely named constant-B projection MatMuls")
    receipt["fp32_node_counts"] = dict(Counter(node.op_type for node in metadata.graph.node))
    receipt["selected_constant_b_matmuls"] = len(selected)
    del metadata, initializers
    started = time.monotonic()
    emit("quantizing", selected_matmuls=len(selected))
    quantize_dynamic(
        str(fp32),
        str(output / INT8),
        nodes_to_quantize=selected,
        op_types_to_quantize=["MatMul"],
        per_channel=True,
        reduce_range=False,
        weight_type=QuantType.QInt8,
        use_external_data_format=True,
        extra_options={"MatMulConstBOnly": True},
    )
    receipt["quantize_seconds"] = time.monotonic() - started
    onnx.checker.check_model(str(output / INT8))
    quantized = onnx.load(output / INT8, load_external_data=False)
    counts = Counter(node.op_type for node in quantized.graph.node)
    receipt["int8_node_counts"] = dict(counts)
    receipt["int8_initializers"] = sum(
        v.data_type == onnx.TensorProto.INT8 for v in quantized.graph.initializer
    )
    if counts["MatMulInteger"] != len(selected) or not receipt["int8_initializers"]:
        raise ValueError("Quantized graph did not preserve the expected integer projection count")
    del quantized
    release_memory()

    options = ort.SessionOptions()
    options.intra_op_num_threads = args.threads
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    receipt["comparisons"] = [
        {key: item[key] for key in ("index", "canonical_speech", "text", "torch_hot_seconds")}
        | {"token_count": item["tokens"]["input_ids"].shape[1]}
        for item in examples
    ]
    for label, filename in (("onnx_fp32", FP32), ("onnx_int8", INT8)):
        started = time.monotonic()
        session = ort.InferenceSession(
            str(output / filename), sess_options=options, providers=["CPUExecutionProvider"]
        )
        receipt[label + "_load_seconds"] = time.monotonic() - started
        warm_started = time.monotonic()
        feature(session, examples[0]["tokens"])
        receipt[label + "_warmup_seconds"] = time.monotonic() - warm_started
        for item, comparison in zip(examples, receipt["comparisons"], strict=True):
            hot_times = []
            for _ in range(args.repetitions):
                started = time.monotonic()
                actual = feature(session, item["tokens"])
                hot_times.append(time.monotonic() - started)
            expected = item["expected"]
            delta = actual.astype(np.float64) - expected.astype(np.float64)
            flat_a, flat_b = actual.reshape(-1).astype(np.float64), expected.reshape(-1).astype(np.float64)
            similarity = float(np.dot(flat_a, flat_b) / (np.linalg.norm(flat_a) * np.linalg.norm(flat_b)))
            comparison[label] = {
                "cosine_similarity": similarity,
                "max_absolute_error": float(np.max(np.abs(delta))),
                "mean_absolute_error": float(np.mean(np.abs(delta))),
                "rmse": float(np.sqrt(np.mean(delta * delta))),
                "hot_seconds": hot_times,
                "hot_median_seconds": float(np.median(hot_times)),
            }
            emit("feature_comparison", backend=label, **comparison)
        del session
        release_memory()
    receipt["files"] = {
        path.name: {"bytes": path.stat().st_size, "sha256": digest(path)}
        for path in output.iterdir()
        if path.is_file() and path.name != "receipt.json"
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/tuber"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if not args.execute or args.threads < 1 or args.repetitions < 1:
        parser.error(
            "Requires --execute, positive thread/repetition counts, and an assigned outer memory-watch window"
        )
    root = args.root.absolute()
    root = checked_path(root, root)
    boundary = checked_path(root / "runtime/evaluations", root)
    output = checked_path(args.output, boundary, exists=False)
    if output.exists():
        parser.error("Output must be a new runtime/evaluations directory; no overwrites")
    base = checked_path(root / "runtime/tts/pretrained/chinese-roberta-wwm-ext-large", root)
    # Identity is material: this candidate must derive from this exact existing
    # production checkpoint, never from another public RoBERTa ONNX bundle.
    source_files = {}
    source_stamps = {}
    for candidate in sorted(base.rglob("*")):
        path = checked_path(candidate, base)
        if path.is_file():
            stat = path.stat()
            source_stamps[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            source_files[str(path.relative_to(base))] = {"bytes": stat.st_size, "sha256": digest(path)}
    if "config.json" not in source_files or not any(
        name.endswith((".bin", ".safetensors")) for name in source_files
    ):
        parser.error("Original local config/checkpoint files are required")
    output.mkdir(parents=True, exist_ok=False)
    (output / "work").mkdir()
    tempfile.tempdir = str(output / "work")
    os.environ["TMPDIR"] = tempfile.tempdir
    receipt = {
        "kind": "original_chinese_bert_22_layer_dynamic_int8",
        "completed": False,
        "source": str(base),
        "source_files": source_files,
        "output": str(output),
        "process_id": os.getpid(),
        "threads": args.threads,
        "configuration": {
            "cpu_float32_export": True,
            "dynamo": False,
            "opset": 20,
            "dynamic_token_length": True,
            "per_channel": True,
            "weight_type": "QInt8",
            "constant_b_matmul_only": True,
            "source_checkpoint_modified": False,
            "runtime_torch_model_required": False,
            "speech_quality_or_speed_verified": False,
        },
    }
    started = time.monotonic()
    try:
        prepare(args, root, base, output, receipt)
        for path, stamp in source_stamps.items():
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != stamp:
                raise RuntimeError("Source checkpoint/config/tokenizer changed during preparation")
        receipt["completed"] = True
    except BaseException as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        receipt["total_seconds"] = time.monotonic() - started
        (output / "receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        emit("finished", completed=receipt["completed"], seconds=receipt["total_seconds"], output=str(output))


if __name__ == "__main__":
    main()
