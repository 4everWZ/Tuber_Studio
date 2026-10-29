"""Prepare two independent INT8 Genie AR graphs; never start an inference server.

Run once in the isolated ORT1.22.1 evaluation environment under an outer memory
watch. Input models must already reference valid expanded FP32 external data.
No dependencies, source weights, VITS, sampling rules or production files change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

GRAPHS = ("t2s_first_stage_decoder_fp32.onnx", "t2s_stage_decoder_fp32.onnx")
PREFIX = "TUBER_GENIE_QUANT "


def emit(stage, **fields):
    print(PREFIX + json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_node(node):
    """Ignore only explicit DEFAULT storage presence after external-data loading.

    ONNX external loading writes data_location=DEFAULT even when the original
    embedded TensorProto omitted that optional default field. Keep its complete
    dtype, dimensions, raw payload, node inputs/outputs and operator attributes.
    """
    import onnx

    result = onnx.NodeProto()
    result.CopyFrom(node)

    def visit(graph_nodes):
        for current in graph_nodes:
            for attr in current.attribute:
                tensors = [attr.t] if attr.HasField("t") else list(attr.tensors)
                for tensor in tensors:
                    if tensor.external_data or tensor.data_location == onnx.TensorProto.EXTERNAL:
                        raise RuntimeError("Protected attribute tensor was not materialized")
                    if tensor.data_location == onnx.TensorProto.DEFAULT:
                        tensor.ClearField("data_location")
                if attr.HasField("g"):
                    visit(attr.g.node)
                for graph in attr.graphs:
                    visit(graph.node)

    visit([result])
    return result.SerializeToString()


def describe_node(node):
    return {
        "name": node.name,
        "op_type": node.op_type,
        "inputs": list(node.input),
        "outputs": list(node.output),
        "attributes": [
            {
                "name": attr.name,
                "type": attr.type,
                "sha256": hashlib.sha256(attr.SerializeToString()).hexdigest(),
                **(
                    {
                        "tensor_dtype": attr.t.data_type,
                        "tensor_shape": list(attr.t.dims),
                        "raw_data_sha256": hashlib.sha256(attr.t.raw_data).hexdigest(),
                    }
                    if attr.HasField("t")
                    else {}
                ),
            }
            for attr in node.attribute
        ],
    }


def fold_projection_transposes(model):
    """Fold only immutable 2D weight transposes consumed as projection B."""
    import numpy as np
    import onnx

    initializers = {tensor.name: tensor for tensor in model.graph.initializer}
    graph_inputs = {value.name for value in model.graph.input}
    consumers = {}
    for node in model.graph.node:
        for index, name in enumerate(node.input):
            consumers.setdefault(name, []).append((node.op_type, index))
    folded, kept, additions, candidates = [], [], [], set()
    for node in model.graph.node:
        source = initializers.get(node.input[0]) if node.op_type == "Transpose" and node.input else None
        users = consumers.get(node.output[0], []) if node.output else []
        if (
            source is None
            or source.name in graph_inputs
            or source.data_type != onnx.TensorProto.FLOAT
            or len(source.dims) != 2
            or len(node.output) != 1
            or not users
            or any(op not in {"MatMul", "Gemm"} or index != 1 for op, index in users)
        ):
            kept.append(node)
            continue
        if node.output[0] in initializers:
            raise ValueError("Constant transpose output collides with an existing initializer")
        attributes = {attr.name: onnx.helper.get_attribute_value(attr) for attr in node.attribute}
        if set(attributes) - {"perm"}:
            raise ValueError("Unexpected constant Transpose attribute")
        perm = tuple(attributes.get("perm", (1, 0)))
        if sorted(perm) != [0, 1]:
            raise ValueError("Invalid 2D constant Transpose permutation")
        original = onnx.numpy_helper.to_array(source)
        transposed = np.array(original.transpose(perm), order="C", copy=True)
        replacement = onnx.numpy_helper.from_array(transposed, name=node.output[0])
        expected = hashlib.sha256(original.transpose(perm).tobytes(order="C")).hexdigest()
        actual = hashlib.sha256(onnx.numpy_helper.to_array(replacement).tobytes(order="C")).hexdigest()
        if expected != actual:
            raise RuntimeError("Constant transpose is not bitwise equivalent")
        folded.append(
            {
                "node": node.name,
                "source_initializer": source.name,
                "output_initializer": replacement.name,
                "perm": list(perm),
                "source_shape": list(source.dims),
                "output_shape": list(replacement.dims),
                "source_value_sha256": hashlib.sha256(original.tobytes(order="C")).hexdigest(),
                "expected_transpose_sha256": expected,
                "actual_initializer_sha256": actual,
                "logical_bytes": transposed.nbytes,
                "bitwise_equivalent": True,
            }
        )
        additions.append(replacement)
        candidates.add(source.name)
    del model.graph.node[:]
    model.graph.node.extend(kept)
    used = {name for node in model.graph.node for name in node.input}
    protected = graph_inputs | {value.name for value in model.graph.output}
    removed = [
        tensor.name
        for tensor in model.graph.initializer
        if tensor.name in candidates and tensor.name not in used | protected
    ]
    remaining = [tensor for tensor in model.graph.initializer if tensor.name not in removed]
    del model.graph.initializer[:]
    model.graph.initializer.extend(remaining)
    model.graph.initializer.extend(additions)
    return {"folded": folded, "removed_unused_source_initializers": removed}


def checked_path(path, boundary, *, exists=True):
    path = path.absolute()
    if not path.is_relative_to(boundary):
        raise ValueError("Evaluation path escaped runtime/evaluations")
    for component in (path, *path.parents):
        if component == boundary:
            break
        if component.is_symlink():
            raise ValueError("Evaluation inputs/outputs cannot use symbolic links")
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(boundary):
        raise ValueError("Resolved evaluation path escaped runtime/evaluations")
    return resolved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Requires --execute and an assigned outer memory-watch window")
    root = Path(os.environ.get("TUBER_ROOT", "/workspace/tuber")).resolve(strict=True)
    boundary = (root / "runtime/evaluations").resolve(strict=True)
    models = checked_path(args.models, boundary)
    output = checked_path(args.output, boundary, exists=False)
    if (
        not models.is_dir()
        or output.exists()
        or output.is_relative_to(models)
        or models.is_relative_to(output)
    ):
        parser.error("Input must be a directory; output must be new and separate from the input tree")
    output.mkdir(parents=True, exist_ok=False)
    scratch = output / "work"
    scratch.mkdir()
    # ORT's shape-inference helpers create temporary model files. Passing a
    # ModelProto and setting its temp root keeps all writes away from models.
    tempfile.tempdir = str(scratch)
    os.environ["TMPDIR"] = str(scratch)
    started = time.monotonic()
    receipt = {
        "kind": "genie_ar_dynamic_int8_preparation",
        "completed": False,
        "models": str(models),
        "output": str(output),
        "process_id": os.getpid(),
        "source_files": {},
        "graphs": [],
        "configuration": {
            "onnxruntime": "1.22.1",
            "op_types_to_quantize": ["MatMul"],
            "constant_initializer_B_only": True,
            "per_channel": True,
            "weight_type": "QInt8",
            "reduce_range": False,
            "use_external_data_format": True,
            "embedding_sampling_vits_changed": False,
            "runtime_integer_execution_verified": False,
            "constant_projection_transpose_folding": True,
            "protected_node_comparison": "Exact protobuf after clearing only optional TensorProto DEFAULT storage presence; all tensor bytes/shape/dtype retained",
        },
    }
    source_stamps = {}

    def identify(path):
        if str(path) not in receipt["source_files"]:
            stat = path.stat()
            source_stamps[path] = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            receipt["source_files"][str(path)] = {"bytes": stat.st_size, "sha256": digest(path)}

    try:
        import onnx
        import onnxruntime as ort
        from onnxruntime.quantization import QuantType, quantize_dynamic

        if ort.__version__ != "1.22.1":
            raise RuntimeError("This candidate requires the reviewed isolated ORT1.22.1")
        receipt["onnx_version"] = onnx.__version__
        for filename in GRAPHS:
            graph_started = time.monotonic()
            source = checked_path(models / filename, models)
            identify(source)
            metadata = onnx.load(source, load_external_data=False)
            for tensor in metadata.graph.initializer:
                if tensor.data_location != onnx.TensorProto.EXTERNAL:
                    continue
                fields = {item.key: item.value for item in tensor.external_data}
                location = Path(fields.get("location", ""))
                if (
                    not str(location)
                    or location.is_absolute()
                    or ".." in location.parts
                    or "\\" in str(location)
                ):
                    raise ValueError("Unsafe external weight location")
                weight = checked_path(models / location, models)
                if not weight.is_file() or tensor.data_type != onnx.TensorProto.FLOAT:
                    raise ValueError("Expected an existing expanded FP32 external weight file")
                offset = int(fields.get("offset", 0))
                length = int(fields.get("length", math.prod(tensor.dims) * 4))
                if (
                    offset < 0
                    or offset % 4
                    or length != math.prod(tensor.dims) * 4
                    or offset + length > weight.stat().st_size
                ):
                    raise ValueError("External FP32 weight range or tensor shape is invalid")
                identify(weight)
            del metadata
            model = onnx.load(source, load_external_data=True)
            folding = fold_projection_transposes(model)
            emit(
                "constant_transposes_folded",
                graph=filename,
                count=len(folding["folded"]),
                removed_unused_source_initializers=len(folding["removed_unused_source_initializers"]),
            )
            onnx.checker.check_model(model)
            initializers = {value.name: value for value in model.graph.initializer}
            input_uses = Counter(name for node in model.graph.node for name in node.input)
            selected = []
            selected_original = []
            targeted_weights = set()
            for index, node in enumerate(model.graph.node):
                if node.op_type not in {"MatMul", "Gemm"} or len(node.input) < 2:
                    continue
                weight = initializers.get(node.input[1])
                if weight is None or weight.data_type != onnx.TensorProto.FLOAT or len(weight.dims) != 2:
                    continue
                if node.op_type == "Gemm":
                    attributes = {attr.name: onnx.helper.get_attribute_value(attr) for attr in node.attribute}
                    if (
                        attributes.get("alpha", 1.0) != 1.0
                        or attributes.get("beta", 1.0) != 1.0
                        or attributes.get("transA", 0) != 0
                    ):
                        raise ValueError(
                            "Nonstandard Gemm is outside the reviewed dynamic-quantization route"
                        )
                    if attributes.get("transB", 0) == 1 and input_uses[weight.name] != 1:
                        raise ValueError("ORT Gemm transpose assumes a uniquely consumed B initializer")
                if not node.name:
                    node.name = f"genie_ar_quant_target_{index}"
                selected.append(node.name + ("_MatMul" if node.op_type == "Gemm" else ""))
                selected_original.append({"name": node.name, "op_type": node.op_type, "weight": weight.name})
                targeted_weights.add(weight.name)
            if not selected or len(selected) != len(set(selected)):
                raise ValueError("Expected uniquely named constant-weight AR projection nodes")
            preserved_nodes = Counter(
                canonical_node(node)
                for node in model.graph.node
                if node.op_type not in {"MatMul", "Gemm"} or node.input[1] not in targeted_weights
            )
            protected_descriptions = {
                canonical_node(node): describe_node(node)
                for node in model.graph.node
                if node.op_type not in {"MatMul", "Gemm"} or node.input[1] not in targeted_weights
            }
            embedding_names = {
                node.input[0]
                for node in model.graph.node
                if node.op_type == "Gather" and node.input[0] in initializers
            }
            embeddings = {
                name: hashlib.sha256(onnx.numpy_helper.to_array(initializers[name]).tobytes()).hexdigest()
                for name in embedding_names
            }
            io_contract = ([v.name for v in model.graph.input], [v.name for v in model.graph.output])
            target = output / filename.replace("_fp32.onnx", "_int8.onnx")
            emit("start_quantization", graph=filename, selected_nodes=len(selected))
            quant_started = time.monotonic()
            quantize_dynamic(
                model,
                target,
                op_types_to_quantize=["MatMul"],
                nodes_to_quantize=selected,
                per_channel=True,
                reduce_range=False,
                weight_type=QuantType.QInt8,
                use_external_data_format=True,
                extra_options={"MatMulConstBOnly": True},
            )
            quant_seconds = time.monotonic() - quant_started
            del model, initializers
            onnx.checker.check_model(str(target))
            quantized = onnx.load(target, load_external_data=True)
            quantized_nodes = Counter(canonical_node(node) for node in quantized.graph.node)
            missing = preserved_nodes - quantized_nodes
            if missing:
                by_name = {}
                for node in quantized.graph.node:
                    by_name.setdefault(node.name, []).append(describe_node(node))
                differences = [
                    {
                        "before": protected_descriptions[raw],
                        "missing_count": count,
                        "after_same_name": by_name.get(protected_descriptions[raw]["name"], []),
                    }
                    for raw, count in missing.items()
                ]
                receipt["guard_failure"] = {"graph": filename, "changed_nodes": differences}
                emit("protected_node_guard_failure", graph=filename, changed_nodes=differences)
                raise RuntimeError(
                    "Protected nodes changed: "
                    + ", ".join(item["before"]["name"] for item in differences[:12])
                )
            if io_contract != (
                [v.name for v in quantized.graph.input],
                [v.name for v in quantized.graph.output],
            ):
                raise RuntimeError("Quantization changed the graph input/output names or order")
            quantized_initializers = {v.name: v for v in quantized.graph.initializer}
            for name, expected in embeddings.items():
                tensor = quantized_initializers.get(name)
                if (
                    tensor is None
                    or hashlib.sha256(onnx.numpy_helper.to_array(tensor).tobytes()).hexdigest() != expected
                ):
                    raise RuntimeError("Embedding initializer changed")
            counts = dict(Counter(node.op_type for node in quantized.graph.node))
            integer_nodes = counts.get("MatMulInteger", 0)
            qint8 = [v for v in quantized.graph.initializer if v.data_type == onnx.TensorProto.INT8]
            if integer_nodes != len(selected) or not qint8:
                raise RuntimeError("Actual integer graph nodes do not match the selected projection count")
            files = [target, target.with_name(target.name + ".data")]
            graph_receipt = {
                "source": filename,
                "output": target.name,
                "quantize_seconds": quant_seconds,
                "total_graph_seconds": time.monotonic() - graph_started,
                "selected_nodes": selected_original,
                "constant_transpose_folding": folding,
                "actual_node_counts": counts,
                "matmul_integer_nodes": integer_nodes,
                "int8_initializer_count": len(qint8),
                "int8_logical_bytes": sum(onnx.numpy_helper.to_array(v).nbytes for v in qint8),
                "embedding_initializers_unchanged": len(embeddings),
                "non_target_node_protobufs_unchanged": sum(preserved_nodes.values()),
                "artifacts": {
                    f.name: {"bytes": f.stat().st_size, "sha256": digest(f)} for f in files if f.exists()
                },
            }
            receipt["graphs"].append(graph_receipt)
            emit("quantized", **graph_receipt)
            del quantized, quantized_initializers, qint8
        for source, before in source_stamps.items():
            stat = source.stat()
            if before != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
                raise RuntimeError("An input model or external weight file changed during preparation")
        receipt["completed"] = True
    except Exception as error:
        receipt["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        if sys.platform.startswith("linux"):
            import resource

            receipt["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        (output / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        emit("receipt", **receipt)


if __name__ == "__main__":
    main()
