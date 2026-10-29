"""Install and validate one permanent, fixed-voice Genie INT8 bundle.

Preparation is explicit and short-lived. Runtime validation never reads an
evaluation directory, converts weights, downloads assets or selects a fallback.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

FORMAT = 1
KIND = "fixed_voice_genie_int8"
REVISION = "d347fd0f8683e9a362b69f59fa0a4799ddb5e828"
ENCODER_TEMPLATE_SHA256 = "f6eb1acd47c8e6d36b777886981a49122e8e070a5eb9888d458fb188dc139f75"
AR_NAMES = ("t2s_first_stage_decoder_int8.onnx", "t2s_stage_decoder_int8.onnx")
IMPLEMENTATIONS = (
    "genie_backend.py",
    "genie_sampling.py",
    "genie_bert.py",
    "genie_bundle.py",
    "genie_quantize.py",
    "compact.py",
    "serve.py",
    "inference_only.py",
)
PATHS = {
    "encoder": "encoder.onnx",
    "first": "ar/" + AR_NAMES[0],
    "stage": "ar/" + AR_NAMES[1],
    "vits": "vits/vits.onnx",
    "bert": "bert/bert_int8.onnx",
    "bert_dir": "bert",
}


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_path(path, boundary, *, exists=True):
    path, boundary = Path(path).absolute(), Path(boundary).absolute()
    if not path.is_relative_to(boundary):
        raise ValueError("Bundle path escaped its permitted directory")
    for component in (path, *path.parents):
        if component.is_symlink() or getattr(component, "is_junction", lambda: False)():
            raise ValueError("Bundle paths cannot use symbolic links or junctions")
    resolved = path.resolve(strict=exists)
    if not resolved.is_relative_to(boundary.resolve(strict=True)):
        raise ValueError("Resolved bundle path escaped its permitted directory")
    return resolved


def stamp(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def file_record(path):
    before = stamp(path)
    result = {"bytes": before[2], "sha256": digest(path)}
    if stamp(path) != before:
        raise ValueError("File changed while its bundle identity was being read")
    return result


def read_json(path):
    if Path(path).stat().st_size > 8_000_000:
        raise ValueError("Bundle metadata exceeds its size limit")
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Bundle metadata must be an object")
    return value


def _module(root, name):
    if name not in {"compact", "genie_sampling"}:
        raise ValueError("Unknown fixed bundle helper")
    path = checked_path(root / "tools/jetson/tts" / (name + ".py"), root)
    spec = importlib.util.spec_from_file_location("_tuber_genie_bundle_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _voice_directory(root, voice):
    if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", voice):
        raise ValueError("Invalid fixed voice ID")
    return checked_path(root / "runtime/tts/genie" / voice, root, exists=False)


def _manifest_identity(manifest):
    core = {key: value for key, value in manifest.items() if key != "bundle_identity"}
    return hashlib.sha256(
        json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_unchanged(signature):
    if not isinstance(signature, dict) or signature.get("format") != FORMAT or not signature.get("files"):
        raise ValueError("Invalid Genie bundle stat signature")
    for name, expected in signature["files"].items():
        path = Path(name)
        try:
            if (
                path.is_symlink()
                or getattr(path, "is_junction", lambda: False)()
                or stamp(path) != tuple(expected)
            ):
                raise ValueError("Genie bundle/source changed; rebuild and reload the fixed voice")
            # A replaced parent link must not redirect an otherwise unchanged inode.
            if path.resolve(strict=True) != path:
                raise ValueError("Genie bundle path changed; reload is required")
        except OSError as error:
            raise ValueError("Genie bundle/source disappeared; explicit preparation is required") from error


def _verify_file(path, record):
    if (
        not isinstance(record, dict)
        or type(record.get("bytes")) is not int
        or not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", ""))
    ):
        raise ValueError("Invalid bundle file identity")
    if file_record(path) != record:
        raise ValueError("Genie bundle file identity mismatch: " + path.name)


def _reference(root, voice):
    compact = _module(root, "compact")
    _, directory, _, identity = compact._settings(root, voice)
    metadata, arrays = compact._read_bundle(directory, identity)
    fixed = compact.reference_signature(root, voice_id=voice, metadata=metadata)
    return compact, directory, identity, metadata, arrays, fixed


def verify_bundle(root, voice_id):
    """Full hashes once at startup; return immutable paths and cheap live guards."""
    root = checked_path(Path(root).absolute(), Path(root).absolute())
    directory = _voice_directory(root, voice_id)
    return _verify_directory(root, voice_id, directory)


def _verify_directory(root, voice_id, directory):
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(
            "Genie voice bundle is missing; run genie_bundle.py explicitly before enabling this backend"
        )
    manifest_path = checked_path(manifest_path, directory)
    initial = stamp(manifest_path)
    manifest = read_json(manifest_path)
    if (
        manifest.get("format") != FORMAT
        or manifest.get("kind") != KIND
        or manifest.get("voice_id") != voice_id
        or manifest.get("genie_revision") != REVISION
        or manifest.get("paths") != PATHS
        or manifest.get("bundle_identity") != _manifest_identity(manifest)
    ):
        raise ValueError("Unsupported or changed Genie voice manifest; rebuild is required")
    source_before = {}
    for raw in manifest.get("source_identity", {}).get("paths", {}).values():
        path = checked_path(raw, root)
        source_before[str(path)] = stamp(path)
    for relative in ("config/voices.json", "runtime/tts/tts_infer.yaml"):
        path = checked_path(root / relative, root)
        source_before[str(path)] = stamp(path)
    _, reference_dir, identity, reference_metadata, arrays, fixed = _reference(root, voice_id)
    if manifest.get("source_identity") != identity:
        raise ValueError("Genie bundle belongs to a different voice/checkpoint/reference/frontend")
    reference = manifest.get("reference", {})
    expected_reference = {
        "metadata": str((reference_dir / "metadata.json").relative_to(root)),
        "features": str((reference_dir / "features.npz").relative_to(root)),
    }
    if any(reference.get(key) != value for key, value in expected_reference.items()):
        raise ValueError("Genie reference must use the controlled compact cache")
    signatures = {str(manifest_path): initial, **source_before}
    paths = {}
    files = manifest.get("files", {})
    required = set(PATHS.values()) - {"bert"}
    required |= {"bert/receipt.json", "bert/bert_int8.onnx.data", "vits/vits_fp32.bin"}
    required |= {"ar/" + name + ".data" for name in AR_NAMES}
    if not isinstance(files, dict) or not required.issubset(files):
        raise ValueError("Genie manifest is missing required runtime artifacts")
    for relative, record in files.items():
        path = checked_path(directory / relative, directory)
        _verify_file(path, record)
        signatures[str(path)] = stamp(path)
    implementations = manifest.get("implementations", {})
    if set(implementations) != set(IMPLEMENTATIONS):
        raise ValueError("Genie implementation identity list is incomplete")
    for filename, record in implementations.items():
        path = checked_path(root / "tools/jetson/tts" / filename, root)
        _verify_file(path, record)
        signatures[str(path)] = stamp(path)
    for key, filename in expected_reference.items():
        path = checked_path(root / filename, root)
        _verify_file(path, reference[key + "_identity"])
        signatures[str(path)] = stamp(path)
    for raw in identity["paths"].values():
        path = checked_path(raw, root)
        if signatures.get(str(path)) != stamp(path):
            raise ValueError("Original voice source changed during validation")
    for relative in identity["source_sha256"]:
        path = checked_path(
            Path(os.environ.get("TUBER_GPT_SOVITS_ROOT", "/opt/GPT-SoVITS-v2")) / relative,
            Path(os.environ.get("TUBER_GPT_SOVITS_ROOT", "/opt/GPT-SoVITS-v2")),
        )
        signatures[str(path)] = stamp(path)
    for path in (root / "config/voices.json", root / "runtime/tts/tts_infer.yaml"):
        signatures[str(checked_path(path, root))] = stamp(path)
    if stamp(manifest_path) != initial:
        raise ValueError("Genie manifest changed during validation")
    signature = {"format": FORMAT, "files": signatures}
    verify_unchanged(signature)
    for name, relative in PATHS.items():
        paths[name] = checked_path(directory / relative, directory)
    paths.update(
        tokenizer=Path(identity["paths"]["bert/config.json"]).parent,
        reference_metadata=reference_dir / "metadata.json",
        reference_features=reference_dir / "features.npz",
    )
    return {
        "directory": directory,
        "paths": paths,
        "metadata": manifest,
        "identity": manifest["bundle_identity"],
        "bundle_identity": manifest["bundle_identity"],
        "reference_metadata": reference_metadata,
        "reference_arrays": arrays,
        "stat_signature": signature,
        "fixed_signature": fixed,
    }


validate_bundle = verify_bundle


def _copy_verified(source, target, record):
    _verify_file(source, record)
    before = stamp(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    if stamp(source) != before:
        raise ValueError("Preparation source changed while copying")
    _verify_file(target, record)


def _graphs(model):
    def visit(graph):
        yield graph
        for node in graph.node:
            for attr in node.attribute:
                if attr.HasField("g"):
                    yield from visit(attr.g)
                for nested in attr.graphs:
                    yield from visit(nested)

    yield from visit(model.graph)


def _external_files(path, boundary):
    """Validate mixed INT8/FP32 external data, including Constant attributes."""
    import onnx

    model = onnx.load(path, load_external_data=False)
    if model.functions or model.training_info or model.graph.sparse_initializer:
        raise ValueError("Unsupported ONNX bundle container")
    files = set()
    for graph in _graphs(model):
        tensors = list(graph.initializer)
        for node in graph.node:
            for attr in node.attribute:
                if attr.HasField("t"):
                    tensors.append(attr.t)
                tensors.extend(attr.tensors)
        for tensor in tensors:
            if tensor.data_location != onnx.TensorProto.EXTERNAL:
                continue
            fields = {v.key: v.value for v in tensor.external_data}
            location = fields.get("location", "")
            if not location or any(c in location for c in "/\\:") or location in {".", ".."}:
                raise ValueError("External tensors must use an adjacent safe basename")
            external = checked_path(Path(path).parent / location, boundary)
            sizes = {
                onnx.TensorProto.FLOAT: 4,
                onnx.TensorProto.INT8: 1,
                onnx.TensorProto.UINT8: 1,
                onnx.TensorProto.INT32: 4,
                onnx.TensorProto.INT64: 8,
                onnx.TensorProto.FLOAT16: 2,
            }
            if tensor.data_type not in sizes:
                raise ValueError("Unsupported external tensor datatype")
            expected = math.prod(tensor.dims) * sizes[tensor.data_type]
            offset, length = int(fields.get("offset", 0)), int(fields.get("length", expected))
            if offset < 0 or length != expected or offset + length > external.stat().st_size:
                raise ValueError("Truncated or invalid external tensor range")
            files.add(external)
    return model, files


def _check_runtime_graph(path, boundary, *, integer_nodes=None, noise=None):
    model, external = _external_files(path, boundary)
    if (
        integer_nodes is not None
        and sum(n.op_type == "MatMulInteger" for n in model.graph.node) != integer_nodes
    ):
        raise ValueError("Quantized projection count changed")
    if noise is not None:
        value = next((i for i in model.graph.input if i.name == noise), None)
        if value is None or value.type.tensor_type.elem_type != 1:
            raise ValueError("Restored sampler input is missing or has the wrong type")
    return model, external


def _check_ar_receipt(receipt):
    expected = {
        "onnxruntime": "1.22.1",
        "op_types_to_quantize": ["MatMul"],
        "constant_initializer_B_only": True,
        "per_channel": True,
        "weight_type": "QInt8",
        "reduce_range": False,
        "use_external_data_format": True,
        "constant_projection_transpose_folding": True,
    }
    if receipt.get("kind") != "genie_ar_dynamic_int8_preparation" or receipt.get("completed") is not True:
        raise ValueError("AR bundle has no successful preparation receipt")
    if any(receipt.get("configuration", {}).get(k) != value for k, value in expected.items()):
        raise ValueError("AR quantization configuration does not match the supported candidate")
    graphs = receipt.get("graphs", [])
    if len(graphs) != 2 or {g.get("output") for g in graphs} != set(AR_NAMES):
        raise ValueError("AR receipt must identify exactly the first and recurrent graphs")
    for graph in graphs:
        if (
            graph.get("matmul_integer_nodes") != 97
            or graph.get("actual_node_counts", {}).get("MatMulInteger") != 97
        ):
            raise ValueError("AR receipt must retain all 97 constant-weight projections")
    return {graph["output"]: graph for graph in graphs}


def _load_checkpoints(identity):
    source = Path(os.environ.get("TUBER_GENIE_ROOT", "/opt/Genie-TTS")).resolve(strict=True)
    package = source / "src/genie_tts"
    template = checked_path(package / "Data/v2/Models/t2s_encoder_fp32.onnx", source)
    if digest(template) != ENCODER_TEMPLATE_SHA256:
        raise ValueError("Expected the fixed d347 Genie encoder template")
    spec = importlib.util.spec_from_file_location(
        "genie_tts", package / "__init__.py", submodule_search_locations=[str(package)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["genie_tts"] = module
    loader_spec = importlib.util.spec_from_file_location(
        "_genie_original_weight_loader", package / "Converter/load_state_dict.py"
    )
    loader = importlib.util.module_from_spec(loader_spec)
    sys.path.insert(0, str(package / "Converter"))
    loader_spec.loader.exec_module(loader)
    return (
        loader.load_gpt_model(identity["paths"]["t2s"])["weight"],
        loader.load_sovits_model(identity["paths"]["vits"])["weight"],
        template,
    )


def _verify_source_weights(models, gpt, vits, template, sampling):
    import onnx
    import torch

    checked = {}
    for filename, kind in (
        ("t2s_encoder_fp32.onnx", "encoder"),
        ("t2s_first_stage_decoder_fp32.onnx", "first"),
        ("t2s_stage_decoder_fp32.onnx", "stage"),
        ("vits_fp32.onnx", "vits"),
    ):
        model, _ = _external_files(models / filename, models)
        expected = (
            sampling._fingerprint(onnx.load(template, load_external_data=False))
            if kind == "encoder"
            else sampling.PINNED[kind]["graph"]
        )
        if sampling._fingerprint(model) != expected:
            raise ValueError("Converted graph differs from its pinned original template")
        count = 0
        for tensor in model.graph.initializer:
            fields = {v.key: v.value for v in tensor.external_data}
            if (
                tensor.data_location != onnx.TensorProto.EXTERNAL
                or tensor.data_type != onnx.TensorProto.FLOAT
            ):
                raise ValueError("Original converted graph must use external FP32 initializers")
            if kind == "encoder":
                key = (
                    "model." + tensor.name.removeprefix("encoder.")
                    if tensor.name.startswith("encoder.")
                    else tensor.name.removeprefix("vits.")
                )
                original = (gpt if tensor.name.startswith("encoder.") else vits)[key]
                expected_tensor = original.detach().cpu().to(torch.float32)
            else:
                key = (
                    tensor.name.removeprefix("vq_model.")
                    if kind == "vits"
                    else "model." + tensor.name.replace("transformer_encoder", "h")
                )
                original = (vits if kind == "vits" else gpt)[key]
                expected_tensor = original.detach().cpu().to(torch.float16).to(torch.float32)
            if tuple(original.shape) != tuple(tensor.dims):
                raise ValueError("Original checkpoint/template tensor shape differs")
            expected_bytes = expected_tensor.numpy().tobytes()
            external = checked_path(models / fields["location"], models)
            with external.open("rb") as stream:
                stream.seek(int(fields.get("offset", 0)))
                actual = stream.read(len(expected_bytes))
            if actual != expected_bytes:
                raise ValueError(
                    "Converted FP32 weights do not match the original checkpoint: " + tensor.name
                )
            count += 1
        checked[filename] = count
    return checked


def _prune_encoder(source, target):
    import onnx

    model = onnx.load(source, load_external_data=True)
    needed, kept = {model.graph.output[0].name}, []
    for node in reversed(model.graph.node):
        if needed.intersection(node.output):
            kept.append(node)
            needed.update(node.input)
    inputs = [value for value in model.graph.input if value.name in needed]
    tensors = [value for value in model.graph.initializer if value.name in needed]
    output = model.graph.output[0]
    del model.graph.node[:]
    model.graph.node.extend(reversed(kept))
    del model.graph.input[:]
    model.graph.input.extend(inputs)
    del model.graph.initializer[:]
    model.graph.initializer.extend(tensors)
    del model.graph.output[:]
    model.graph.output.extend([output])
    onnx.save_model(model, target)
    onnx.checker.check_model(str(target))


def _publish(stage, final):
    """Atomic, no-replace directory publication on the supported target hosts."""
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ValueError("Atomic no-replace rename is unavailable")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        if rename(-100, os.fsencode(stage), -100, os.fsencode(final), 1) != 0:
            raise OSError(
                ctypes.get_errno(), "Cannot publish Genie bundle without replacing an existing path"
            )
    elif os.name == "nt":
        os.rename(stage, final)
    else:
        raise ValueError("Atomic no-replace publication is unsupported on this host")


def install_bundle(root, voice, models, ar_bundle, bert_bundle):
    root = checked_path(Path(root).absolute(), Path(root).absolute())
    evaluation = checked_path(root / "runtime/evaluations", root)
    models, ar_bundle, bert_bundle = [
        checked_path(value, evaluation) for value in (models, ar_bundle, bert_bundle)
    ]
    final = _voice_directory(root, voice)
    if final.exists():
        raise ValueError(
            "A permanent voice bundle already exists; explicit rebuild is required, no overwrite"
        )
    final.parent.mkdir(parents=True, exist_ok=True)
    stage = final.parent / ("." + voice + ".stage-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    source_stamps = {}

    def source(path):
        path = checked_path(path, root)
        current = stamp(path)
        if str(path) in source_stamps and source_stamps[str(path)] != current:
            raise ValueError("Preparation source changed during installation")
        source_stamps.setdefault(str(path), current)
        return path

    try:
        compact, reference_dir, identity, reference_metadata, _, fixed = _reference(root, voice)
        for path in (reference_dir / "metadata.json", reference_dir / "features.npz"):
            source(path)
        sampling = _module(root, "genie_sampling")
        implementations = {
            name: file_record(source(root / "tools/jetson/tts" / name)) for name in IMPLEMENTATIONS
        }
        for raw in identity["paths"].values():
            source(raw)
        scratch = stage / "scratch"
        scratch.mkdir()
        gpt, vits, template = _load_checkpoints(identity)
        mapping = _verify_source_weights(models, gpt, vits, template, sampling)
        del gpt, vits
        for filename in (
            "t2s_encoder_fp32.onnx",
            "t2s_first_stage_decoder_fp32.onnx",
            "t2s_stage_decoder_fp32.onnx",
            "vits_fp32.onnx",
        ):
            _, external = _external_files(source(models / filename), models)
            for path in external:
                source(path)
        ar_receipt = read_json(source(ar_bundle / "receipt.json"))
        ar_graphs = _check_ar_receipt(ar_receipt)
        source_records = ar_receipt.get("source_files", {})
        for label, name in (("first", AR_NAMES[0]), ("stage", AR_NAMES[1])):
            graph_record = ar_graphs[name]
            original_name = graph_record["source"]
            if original_name != name.replace("_int8.onnx", "_fp32.onnx"):
                raise ValueError("AR receipt source graph identity differs")
            # Recreate the exact restored graph in the original source directory's
            # staged sibling. External files are explicitly copied and verified.
            for path in _external_files(models / original_name, models)[1]:
                if not (scratch / path.name).exists():
                    _copy_verified(source(path), scratch / path.name, file_record(path))
            patched = scratch / original_name
            patch = sampling.patch_graph(models / original_name, patched, label)
            if source_records.get(str((models / original_name).resolve())) == file_record(
                models / original_name
            ):
                # The quantizer must have consumed the restored graph, not the
                # original Normal/TopK15 graph with the same apparent filename.
                raise ValueError("AR quantizer receipt points to an un-restored original graph")
            matching = [record for raw, record in source_records.items() if Path(raw).name == original_name]
            if len(matching) != 1 or matching[0] != file_record(patched):
                raise ValueError("AR quantization source does not match restored Exp/TopK5/EOS graph")
            for path in _external_files(patched, scratch)[1]:
                matches = [record for raw, record in source_records.items() if Path(raw).name == path.name]
                if len(matches) != 1 or matches[0] != file_record(path):
                    raise ValueError("AR quantization weights differ from the original checkpoint mapping")
            model, external = _check_runtime_graph(
                source(ar_bundle / name), ar_bundle, integer_nodes=97, noise=sampling.EXP_INPUT
            )
            if model.graph.input[-1].name != sampling.EXP_INPUT:
                raise ValueError("Unexpected restored AR noise interface")
            for path in {ar_bundle / name, *external}:
                record = graph_record.get("artifacts", {}).get(path.name)
                if record is None:
                    raise ValueError("AR receipt lacks an external artifact identity")
                _copy_verified(source(path), stage / "ar" / path.name, record)
            graph_record["sampling_source_sha256"] = patch["output_sha256"]
        bert_receipt = read_json(source(bert_bundle / "receipt.json"))
        config = bert_receipt.get("configuration", {})
        if (
            bert_receipt.get("kind") != "original_chinese_bert_22_layer_dynamic_int8"
            or bert_receipt.get("completed") is not True
            or bert_receipt.get("effective_layers") != 22
            or bert_receipt.get("original_layers") != 24
            or bert_receipt.get("selected_constant_b_matmuls") != 132
            or bert_receipt.get("int8_node_counts", {}).get("MatMulInteger") != 132
            or bert_receipt.get("source_hidden_state") != "original hidden_states[-3], i.e. layer 22 output"
            or bert_receipt.get("versions", {}).get("onnxruntime") != "1.22.1"
            or any(
                config.get(key) != value
                for key, value in {
                    "cpu_float32_export": True,
                    "dynamo": False,
                    "dynamic_token_length": True,
                    "per_channel": True,
                    "weight_type": "QInt8",
                    "constant_b_matmul_only": True,
                }.items()
            )
        ):
            raise ValueError(
                "BERT receipt does not identify the original 22-layer dynamic INT8 representation"
            )
        current_bert = {
            key.removeprefix("bert/"): value
            for key, value in identity["sha256"].items()
            if key.startswith("bert/")
        }
        declared_bert = bert_receipt.get("source_files", {})
        if set(current_bert) != set(declared_bert) or any(
            declared_bert[key].get("sha256") != value for key, value in current_bert.items()
        ):
            raise ValueError("BERT was prepared from another checkpoint/config/tokenizer")
        bert_base = Path(identity["paths"]["bert/config.json"]).parent
        if any(declared_bert[key].get("bytes") != (bert_base / key).stat().st_size for key in current_bert):
            raise ValueError("BERT source-file sizes do not match its original checkpoint")
        _, external = _check_runtime_graph(
            source(bert_bundle / "bert_int8.onnx"), bert_bundle, integer_nodes=132
        )
        for path in {bert_bundle / "bert_int8.onnx", *external}:
            record = bert_receipt.get("files", {}).get(path.name)
            if record is None:
                raise ValueError("BERT receipt lacks an artifact identity")
            _copy_verified(source(path), stage / "bert" / path.name, record)
        bert_receipt["source"], bert_receipt["output"] = "original_checkpoint_and_tokenizer", "."
        (stage / "bert/receipt.json").write_text(
            json.dumps(bert_receipt, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        _prune_encoder(models / "t2s_encoder_fp32.onnx", stage / PATHS["encoder"])
        (stage / "vits").mkdir()
        _copy_verified(
            source(models / "vits_fp32.bin"),
            stage / "vits/vits_fp32.bin",
            file_record(models / "vits_fp32.bin"),
        )
        vits_patch = sampling.patch_graph(models / "vits_fp32.onnx", stage / PATHS["vits"], "vits")
        _check_runtime_graph(stage / PATHS["vits"], stage / "vits", noise=sampling.NORMAL_INPUT)
        shutil.rmtree(scratch)
        manifest = {
            "format": FORMAT,
            "kind": KIND,
            "voice_id": voice,
            "genie_revision": REVISION,
            "paths": PATHS,
            "source_identity": identity,
            "reference": {
                "metadata": str((reference_dir / "metadata.json").relative_to(root)),
                "features": str((reference_dir / "features.npz").relative_to(root)),
                "metadata_identity": file_record(reference_dir / "metadata.json"),
                "features_identity": file_record(reference_dir / "features.npz"),
            },
            "implementations": implementations,
            "preparation": {
                "original_checkpoint_initializers_verified": mapping,
                "ar_configuration": ar_receipt["configuration"],
                "ar_graphs": [
                    {
                        key: g[key]
                        for key in (
                            "output",
                            "matmul_integer_nodes",
                            "int8_logical_bytes",
                            "sampling_source_sha256",
                        )
                    }
                    for g in ar_receipt["graphs"]
                ],
                "bert_configuration": config,
                "bert_layers": 22,
                "bert_projection_count": 132,
                "vits_sampling": vits_patch["sampling"],
            },
            "files": {
                str(path.relative_to(stage)): file_record(path)
                for path in sorted(stage.rglob("*"))
                if path.is_file()
            },
        }
        manifest["bundle_identity"] = _manifest_identity(manifest)
        with (stage / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for path in (reference_dir / "metadata.json", reference_dir / "features.npz"):
            source(path)
        if compact.reference_signature(root, voice_id=voice, metadata=reference_metadata) != fixed:
            raise ValueError("Original voice/reference changed during installation")
        verify_unchanged({"format": FORMAT, "files": source_stamps})
        _verify_directory(root, voice, stage)
        _publish(stage, final)
        return verify_bundle(root, voice)
    finally:
        if stage.exists():
            checked_path(stage, final.parent)
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/tuber"))
    parser.add_argument("--voice", required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--ar-bundle", type=Path, required=True)
    parser.add_argument("--bert-bundle", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Requires --execute and an exclusive offline preparation window")
    bundle = install_bundle(args.root, args.voice, args.models, args.ar_bundle, args.bert_bundle)
    print(
        json.dumps(
            {
                "completed": True,
                "directory": str(bundle["directory"]),
                "bundle_identity": bundle["bundle_identity"],
            }
        )
    )


if __name__ == "__main__":
    main()
