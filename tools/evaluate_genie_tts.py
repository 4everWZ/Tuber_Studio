"""Isolated Genie v2 CPU backend with original dynamic Chinese frontend.

No dependency installs or production configuration writes happen here. Only the
explicit coexistence switch performs a fixed typed broker warmup; the maintenance
controller owns the exclusive window and Mini restoration. Genie templates/converters are MIT High_Logic (2025); stock
weight loading executes the unchanged upstream function from its source AST.
The existing checked fixed-reference cache replaces only the reference-only
HuBERT/quantizer encoder branch. Target text is never precomputed between calls.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import logging
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

PREFIX = "TUBER_GENIE "
STARTED = time.monotonic()


def emit(stage, **fields):
    print(
        PREFIX
        + json.dumps(
            {"stage": stage, "elapsed_seconds": time.monotonic() - STARTED, **fields}, ensure_ascii=False
        ),
        flush=True,
    )


def configuration(args):
    return {
        name: getattr(args, name)
        for name in (
            "variant",
            "bert_mode",
            "seed",
            "torch_threads",
            "intra_op_threads",
            "inter_op_threads",
            "cpu_arena",
            "memory_pattern",
            "disable_prepacking",
            "shared_cpu_arena",
            "arena_extend_strategy",
            "warmup_text",
            "case_order",
            "repetitions",
            "load_llm_after_warmup",
            "advise_converted_cache_before_llm",
            "sampling_mode",
            "ar_mode",
        )
    }


def evaluation_adapter(name, root):
    if name not in {"genie_sampling", "genie_bert"}:
        raise ValueError("Unknown fixed evaluation adapter")
    path = Path(__file__).with_name(name + ".py")
    if not path.is_file():
        path = root / "tools/jetson/tts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observe_mini(root, *, load=False, expected=None):
    """Read-only identity checks after an optional fixed typed broker warmup."""
    import httpx
    import psutil

    headers = {"X-Tuber-Inference-Key": (root / "runtime/inference-token").read_text().strip()}
    with httpx.Client(base_url="http://127.0.0.1:9012", headers=headers, trust_env=False) as client:
        if load:
            response = client.post("/llm/2b/warmup", json={}, timeout=180)
            response.raise_for_status()
        response = client.get("/health", timeout=5)
        response.raise_for_status()
        health = response.json()
        response = client.get("/llm/2b/props", timeout=5)
        response.raise_for_status()
        props = response.json()
    providers = health.get("providers", {})
    mini = providers.get("llm2b", {})
    aliases = props.get("model_alias")
    aliases = [aliases] if isinstance(aliases, str) else aliases
    if (
        health.get("mode") != "text"
        or health.get("selected_provider") != "llm2b"
        or health.get("busy") is not False
        or health.get("error")
        or health.get("kv_cache_type") != "q4_0"
        or mini.get("running") is not True
        or mini.get("native_context_tokens") != 131072
        or mini.get("cache_type_k") != "q4_0"
        or mini.get("cache_type_v") != "q4_0"
        or any(
            value.get("running") is not False
            for key, value in providers.items()
            if key not in {"llm2b", "tts"}
        )
        or providers.get("tts", {}).get("running") is not False
        or props.get("default_generation_settings", {}).get("n_ctx") != 131072
        or props.get("is_sleeping") is not False
        or "MiniCPM5-2B" not in (aliases or [])
    ):
        raise RuntimeError(
            "Experimental coexistence requires idle text MiniCPM5-2B/native131072/q4_0, production TTS and other providers stopped"
        )
    matches = [p for p in psutil.process_iter() if p.name() == "llama-server"]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one actual Mini llama-server process")
    process = matches[0]
    argv = process.cmdline()
    identity = {
        "pid": process.pid,
        "uid": process.uids().real,
        "create_time": process.create_time(),
        "argv": argv,
    }
    required = {
        "--alias": "MiniCPM5-2B",
        "--ctx-size": "131072",
        "--cache-type-k": "q4_0",
        "--cache-type-v": "q4_0",
    }
    if (
        identity["uid"] != 1001
        or not any(Path(value).name == "MiniCPM5-2B-Q4_K_M.gguf" for value in argv)
        or any(
            flag not in argv or argv.index(flag) + 1 >= len(argv) or argv[argv.index(flag) + 1] != value
            for flag, value in required.items()
        )
        or (expected is not None and identity != expected)
    ):
        raise RuntimeError("Actual Mini process identity/profile changed or does not match native128K/q4_0")
    return {
        "identity": identity,
        "health": health,
        "props": props,
        "genie_pid": os.getpid(),
        "scope": "Experimental physical coexistence; broker remains text, not formal live mode",
    }


def advise_converted_cache(args):
    """Advise only this isolated bundle's clean files, never global caches."""
    directories = {args.models or args.output / "models", args.ar_models, args.bert_models} - {None}
    details = []
    for directory in sorted(directories):
        advised = 0
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix not in {".bin", ".onnx", ".data"}:
                continue
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
                advised += 1
            finally:
                os.close(descriptor)
        details.append({"directory": str(directory), "files": advised})
    emit("converted_cache_advice", directories=details, global_cache_reclaim=False)


def read_events(path):
    events = []
    for line in path.read_text().splitlines(keepends=True):
        if line.startswith(PREFIX) and line.endswith("\n"):
            events.append(json.loads(line[len(PREFIX) :]))
    return events


def namespace(source):
    spec = importlib.util.spec_from_file_location(
        "genie_tts",
        source / "src/genie_tts/__init__.py",
        submodule_search_locations=[str(source / "src/genie_tts")],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["genie_tts"] = module


def convert(args):
    import onnx
    import torch
    import yaml

    torch.set_num_threads(args.torch_threads)
    namespace(args.source)
    conf = yaml.safe_load((args.root / "runtime/tts/tts_infer.yaml").read_text())["custom"]
    if conf["version"] != "v2":
        raise RuntimeError("Only the unchanged existing v2 voice is permitted")
    output = args.output / "models"
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("Conversion output must be new and empty")
    output.mkdir(exist_ok=True)
    # Deliberately select v2 explicitly: upstream's generic selector guesses
    # architecture from a 150MiB file-size threshold.
    converter = importlib.import_module("genie_tts.Converter.v2.Converter")
    started = time.monotonic()
    converter.convert(conf["t2s_weights_path"], conf["vits_weights_path"], str(output))
    files = list(output.glob("*"))
    if len(files) != 7:
        raise RuntimeError("Genie conversion failed or did not produce the seven v2 files")
    # Shape-check all externally mapped initializers against the original
    # checkpoint, rather than trusting successful byte concatenation.
    loader = importlib.import_module("genie_tts.Converter.load_state_dict")
    gpt = loader.load_gpt_model(conf["t2s_weights_path"])["weight"]
    vits = loader.load_sovits_model(conf["vits_weights_path"])["weight"]
    checked = 0
    for name in ("t2s_first_stage_decoder_fp32.onnx", "t2s_stage_decoder_fp32.onnx", "vits_fp32.onnx"):
        model = onnx.load(output / name, load_external_data=False)
        for tensor in model.graph.initializer:
            if tensor.data_location != onnx.TensorProto.EXTERNAL:
                continue
            key = (
                ("model." + tensor.name.replace("transformer_encoder", "h"))
                if name.startswith("t2s")
                else tensor.name.removeprefix("vq_model.")
            )
            original = (gpt if name.startswith("t2s") else vits)[key]
            if tuple(tensor.dims) != tuple(original.shape):
                raise RuntimeError(f"Checkpoint/template shape mismatch: {name}:{key}")
            checked += 1
    emit(
        "converted",
        seconds=time.monotonic() - started,
        mapped_initializers_shape_checked=checked,
        files={p.name: p.stat().st_size for p in files},
    )


def pruned_encoder(args):
    import onnx

    # Keep exactly the text-conditioning output; reference semantic tokens
    # come from the existing validated cache, not a second reference encoder.
    source = (args.models or args.output / "models") / "t2s_encoder_fp32.onnx"
    model = onnx.load(source)
    needed = {model.graph.output[0].name}
    selected = []
    for node in reversed(model.graph.node):
        if needed.intersection(node.output):
            selected.append(node)
            needed.update(node.input)
    nodes = list(reversed(selected))
    inputs = [i for i in model.graph.input if i.name in needed]
    initializers = [i for i in model.graph.initializer if i.name in needed]
    output = model.graph.output[0]
    del model.graph.node[:]
    model.graph.node.extend(nodes)
    del model.graph.input[:]
    model.graph.input.extend(inputs)
    del model.graph.initializer[:]
    model.graph.initializer.extend(initializers)
    del model.graph.output[:]
    model.graph.output.extend([output])
    path = args.output / "text_encoder_cached_reference.onnx"
    onnx.save(model, path)
    onnx.checker.check_model(str(path))
    return path


def original_frontend(args):
    import torch
    from jetson.tts import compact
    from transformers import AutoTokenizer

    upstream = compact._upstream(args.root)
    base = args.root / "runtime/tts/pretrained/chinese-roberta-wwm-ext-large"
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    if args.bert_mode == "onnx-int8":
        import onnxruntime as ort

        adapter = evaluation_adapter("genie_bert", args.root)
        options = ort.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = (
            args.intra_op_threads,
            args.inter_op_threads,
        )
        if args.shared_cpu_arena:
            options.add_session_config_entry("session.use_env_allocators", "1")
        if args.cpu_arena != "default":
            options.enable_cpu_mem_arena = args.cpu_arena == "on"
        if args.memory_pattern != "default":
            options.enable_mem_pattern = args.memory_pattern == "on"
        if args.disable_prepacking:
            options.add_session_config_entry("session.disable_prepacking", "1")
        session = adapter.load_bert_session(args.bert_models, options=options)
        pre = upstream.TextPreprocessor(session, tokenizer, torch.device("cpu"))

        def onnx_feature(self, text, word2ph):
            hidden = adapter.feature(self.bert_model, self.tokenizer(text, return_tensors="np"))[0][1:-1]
            if len(word2ph) != len(text) or hidden.shape != (len(text), 1024):
                raise RuntimeError("Invalid dynamic original-tokenizer ONNX BERT features")
            hidden = torch.from_numpy(hidden)
            return torch.cat([hidden[i].repeat(word2ph[i], 1) for i in range(len(word2ph))]).T

        pre.get_bert_feature = types.MethodType(onnx_feature, pre)
        emit(
            "bert_storage",
            mode=args.bert_mode,
            layers=22,
            resident_pytorch_bert=False,
            bundle=str(args.bert_models),
            integer_operator_dispatch_verified=False,
        )
        return pre
    from transformers import AutoModelForMaskedLM

    masked = AutoModelForMaskedLM.from_pretrained(base, torch_dtype=torch.float32, local_files_only=True)
    if masked.config.model_type != "bert" or len(masked.bert.encoder.layer) != 24:
        raise RuntimeError("The original 24-layer BERT checkpoint is required")
    backbone = masked.bert
    backbone.encoder.layer = torch.nn.ModuleList(list(backbone.encoder.layer)[:22])
    backbone.config.num_hidden_layers = 22
    del masked
    before_bytes = sum(p.numel() * p.element_size() for p in backbone.parameters())
    if args.bert_mode == "qnnpack-int8":
        if "qnnpack" not in torch.backends.quantized.supported_engines:
            raise RuntimeError("Original Torch has no qnnpack quantized engine")
        torch.backends.quantized.engine = "qnnpack"
        backbone = torch.ao.quantization.quantize_dynamic(
            backbone.eval(), {torch.nn.Linear}, dtype=torch.qint8, inplace=True
        )
    packed_bytes = 0
    quantized_linears = 0
    for module in backbone.modules():
        if isinstance(module, torch.ao.nn.quantized.dynamic.Linear):
            weight = module.weight()
            if weight.dtype != torch.qint8:
                raise RuntimeError("Dynamic BERT Linear has no genuine qint8 weight storage")
            packed_bytes += weight.numel() * weight.element_size()
            quantized_linears += 1
    emit(
        "bert_storage",
        mode=args.bert_mode,
        layers=22,
        fp32_parameter_bytes_before_quantization=before_bytes,
        remaining_parameter_bytes=sum(p.numel() * p.element_size() for p in backbone.parameters()),
        unpacked_qint8_weight_bytes=packed_bytes,
        quantized_linears=quantized_linears,
        integer_operator_dispatch_verified=False,
        note="Logical tensor bytes, not physical RSS or backend packed allocation size; no profiler in formal run",
    )
    pre = upstream.TextPreprocessor(backbone.eval(), tokenizer, torch.device("cpu"))

    def feature(self, text, word2ph):
        with torch.inference_mode():
            hidden = self.bert_model(
                **self.tokenizer(text, return_tensors="pt"), output_hidden_states=False, return_dict=True
            ).last_hidden_state[0][1:-1]
        if len(word2ph) != len(text) or not torch.isfinite(hidden).all():
            raise RuntimeError("Invalid dynamic Chinese BERT features")
        return torch.cat([hidden[i].repeat(word2ph[i], 1) for i in range(len(word2ph))]).T

    pre.get_bert_feature = types.MethodType(feature, pre)
    return pre


def stock_function(source):
    from typing import Optional

    import numpy as np
    import onnx
    import onnxruntime as ort

    tree = ast.parse((source / "src/genie_tts/ModelManager.py").read_text(encoding="utf-8"))
    node = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "load_session_with_fp16_conversion"
    )
    module = ast.Module(body=[node], type_ignores=[])
    scope = {
        "os": os,
        "np": np,
        "onnx": onnx,
        "InferenceSession": ort.InferenceSession,
        "onnxruntime": ort,
        "List": list,
        "Optional": Optional,
        "logger": logging.getLogger(__name__),
    }
    exec(compile(module, str(source / "src/genie_tts/ModelManager.py"), "exec"), scope)
    return scope[node.name]


def run(args):
    import numpy as np
    import onnx
    import onnxruntime as ort
    import soundfile as sf
    import torch
    from evaluate_tts_memory import TEXTS
    from jetson.tts import compact

    torch.set_num_threads(args.torch_threads)
    if args.seed is not None:
        import random

        random.seed(args.seed)
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        ort.set_seed(args.seed)
    emit(
        "protocol",
        options=configuration(args),
        onnxruntime=ort.__version__,
        torch=torch.__version__,
        seed_scope=(
            "Reference mode: per-utterance PCG64 seed with separate segment/AR/VITS substreams; repeat requests reset owned noise. Not Torch seed-sequence equivalence."
            if args.sampling_mode == "reference"
            else "Legacy mode: set process RNGs once before sessions. ONNX node seeds override global seed; existing session RNG advances across calls. No per-utterance reset or Torch/ORT equivalence."
        ),
        target_features_precomputed=False,
    )
    if args.shared_cpu_arena:
        memory_info = ort.OrtMemoryInfo(
            "Cpu", ort.OrtAllocatorType.ORT_ARENA_ALLOCATOR, 0, ort.OrtMemType.DEFAULT
        )
        arena = (
            ort.OrtArenaCfg({"arena_extend_strategy": 1})
            if args.arena_extend_strategy == "same-as-requested"
            else None
        )
        ort.create_and_register_allocator(memory_info, arena)
    namespace(args.source)
    # Load only inference core: skip the public package's asset prompts,
    # optional language frontends, server and audio playback imports.
    dummy = types.ModuleType("genie_tts.Audio.ReferenceAudio")
    dummy.ReferenceAudio = object
    sys.modules[dummy.__name__] = dummy
    feature_dummy = types.ModuleType("genie_tts.GetPhonesAndBert")
    feature_dummy.get_phones_and_bert = None
    sys.modules[feature_dummy.__name__] = feature_dummy
    core = importlib.import_module("genie_tts.Core.Inference")
    _, directory, _, identity = compact._settings(args.root, args.voice)
    metadata, cache = compact._read_bundle(directory, identity)
    signature = compact.reference_signature(args.root, voice_id=args.voice, metadata=metadata)
    emit(
        "reference_precomputed",
        reference_cache=metadata,
        new_reference_precompute=False,
        dynamic_target_features=True,
        resident_hubert=False,
    )
    load_started = time.monotonic()
    emit("bert_loading")
    frontend = original_frontend(args)
    emit("bert_loaded", seconds=time.monotonic() - load_started, layers=22, mode=args.bert_mode, device="cpu")
    stock = stock_function(args.source)
    modeldir = args.models or args.output / "models"
    held = {}

    def shared_weights(binname):
        if binname not in held:
            expanded = np.fromfile(modeldir / binname, dtype=np.float16).astype(np.float32)
            external_path = modeldir / binname.replace("fp16", "fp32")
            expanded.tofile(external_path)
            del expanded
            held[binname] = {"array": np.memmap(external_path, mode="r", dtype=np.float32), "values": []}
        return held[binname]

    def session(path, binname=None):
        options = ort.SessionOptions()
        options.intra_op_num_threads = args.intra_op_threads
        options.inter_op_num_threads = args.inter_op_threads
        if args.cpu_arena != "default":
            options.enable_cpu_mem_arena = args.cpu_arena == "on"
        if args.memory_pattern != "default":
            options.enable_mem_pattern = args.memory_pattern == "on"
        if args.disable_prepacking:
            options.add_session_config_entry("session.disable_prepacking", "1")
        if args.shared_cpu_arena:
            options.add_session_config_entry("session.use_env_allocators", "1")
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        graph = onnx.load(path, load_external_data=False).graph
        random_nodes = [
            {
                "name": node.name,
                "op_type": node.op_type,
                "seed_attributes": [
                    onnx.helper.get_attribute_value(a) for a in node.attribute if a.name == "seed"
                ],
            }
            for node in graph.node
            if node.op_type
            in {"RandomNormal", "RandomNormalLike", "RandomUniform", "RandomUniformLike", "Multinomial"}
        ]
        emit(
            "onnx_random_nodes",
            file=path.name,
            nodes=random_nodes,
            note="Top-level graph inventory; subgraph RNG attributes are not inferred",
        )
        emit("session_loading", file=path.name, variant=args.variant)
        if binname is None:
            return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=options)
        binpath = modeldir / binname
        if args.variant == "stock":
            return stock(str(path), str(binpath), ["CPUExecutionProvider"], options)
        # One immutable FP32 buffer per bin, shared across the two AR sessions.
        # Keep arrays/OrtValues alive for the complete session lifetime.
        shared_weights(binname)
        model = onnx.load(path, load_external_data=False)
        for tensor in model.graph.initializer:
            if tensor.data_location != onnx.TensorProto.EXTERNAL:
                continue
            entries = {v.key: v.value for v in tensor.external_data}
            offset, length = int(entries.get("offset", 0)), int(entries["length"])
            data = held[binname]["array"][offset // 4 : (offset + length) // 4].reshape(tuple(tensor.dims))
            value = ort.OrtValue.ortvalue_from_numpy(data)
            held[binname]["values"].append(value)
            options.add_initializer(tensor.name, value)
        # ORT graph optimization reads initializer TensorProto data before the
        # session-state overrides are installed. Keep valid external data on
        # disk; an empty TensorProto is rejected even with add_initializer.
        return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=options)

    sampling = evaluation_adapter("genie_sampling", args.root) if args.sampling_mode == "reference" else None

    def prepared_graph(filename, kind):
        path = modeldir / filename
        if sampling is not None:
            shared_weights("vits_fp16.bin" if kind == "vits" else "t2s_shared_fp16.bin")
            output = modeldir / (path.stem + "." + args.output.name + ".reference.onnx")
            emit("sampling_restored", **sampling.patch_graph(path, output, kind))
            path = output
        return path

    encoder = session(pruned_encoder(args))
    vocoder = session(prepared_graph("vits_fp32.onnx", "vits"), "vits_fp16.bin")
    if args.ar_mode == "int8":
        receipt = json.loads((args.ar_models / "receipt.json").read_text())
        if not receipt.get("completed"):
            raise RuntimeError("INT8 AR preparation did not complete")
        ar_paths = [
            args.ar_models / name
            for name in ("t2s_first_stage_decoder_int8.onnx", "t2s_stage_decoder_int8.onnx")
        ]
        for path in ar_paths:
            graph = onnx.load(path, load_external_data=False).graph
            if sampling.EXP_INPUT not in {i.name for i in graph.input}:
                raise RuntimeError("INT8 AR graph has not restored original sampling")
            if not any(n.op_type == "MatMulInteger" for n in graph.node):
                raise RuntimeError("INT8 AR graph has no actual integer MatMul nodes")
        first, stage = [session(path) for path in ar_paths]
        emit(
            "ar_quantized",
            preparation_path=str(args.ar_models / "receipt.json"),
            graphs=[
                {key: graph.get(key) for key in ("output", "matmul_integer_nodes", "int8_logical_bytes")}
                for graph in receipt.get("graphs", [])
            ],
            configuration=receipt.get("configuration"),
        )
    else:
        first = session(prepared_graph("t2s_first_stage_decoder_fp32.onnx", "first"), "t2s_shared_fp16.bin")
        stage = session(prepared_graph("t2s_stage_decoder_fp32.onnx", "stage"), "t2s_shared_fp16.bin")
    emit(
        "loaded",
        seconds=time.monotonic() - load_started,
        providers=stage.get_providers(),
        dynamic_bert_resident=True,
        variant=args.variant,
    )
    audio, sr = sf.read(identity["paths"]["reference"], dtype="float32")
    if audio.ndim != 1 or sr != 32000:
        raise RuntimeError("Expected the existing mono32k reference; no implicit resampling")
    prompts = cache["prompt_semantic"].astype(np.int64)
    if prompts.ndim == 1:
        prompts = prompts[None, :]

    class CachedEncoder:
        def run(self, names, feed):
            return [encoder.run(None, {k: v for k, v in feed.items() if k != "ssl_content"})[0], prompts]

    cases = [(rep, index, text) for rep in range(args.repetitions) for index, text in enumerate(TEXTS)]
    if args.case_order == "text-first":
        cases.sort(key=lambda item: (item[1], item[0]))
    if args.warmup_text:
        cases.insert(0, (-1, -1, args.warmup_text))
    mini_identity = None
    for repetition, index, text in cases:
        if compact.reference_signature(args.root, voice_id=args.voice, metadata=metadata) != signature:
            raise RuntimeError("Original voice/reference configuration changed")
        emit("start_speech", repetition=repetition, index=index, text=text)
        started = time.monotonic()
        # Match this deployed v2 pipeline's original call (including the
        # frontend's default phoneme version), rather than Genie's G2P.
        items = frontend.preprocess(text, "zh", "cut5")
        frontend_seconds = time.monotonic() - started
        if not items:
            raise RuntimeError("No dynamic Chinese text features")
        outputs = []
        segment_diagnostics = []
        semantic_seconds, vocoder_seconds = 0.0, 0.0
        sampler = (
            sampling.SamplingSessions(first, stage, vocoder, seed=args.seed if args.seed is not None else -1)
            if sampling
            else None
        )
        for segment_index, item in enumerate(items):
            phones = np.asarray([item["phones"]], dtype=np.int64)
            bert = item["bert_features"].T.contiguous().numpy().astype(np.float32)
            if not np.any(bert) or not np.isfinite(bert).all():
                raise RuntimeError("Chinese BERT features are zero/nonfinite")
            segment_started = time.monotonic()
            selected_vocoder = vocoder
            if sampler is not None:
                selected = sampler.segment(segment_index)
                try:
                    semantic, diagnostics = sampling.decode_segment(
                        CachedEncoder(),
                        selected.first,
                        selected.stage,
                        ref_seq=cache["reference_phones"].astype(np.int64)[None, :],
                        ref_bert=cache["reference_bert"].T.astype(np.float32),
                        text_seq=phones,
                        text_bert=bert,
                        ssl_content=np.empty((0,), np.float32),
                        segment_index=segment_index,
                    )
                except sampling.IncompleteSynthesis as error:
                    emit("segment_failed", **error.diagnostics)
                    raise
                segment_diagnostics.append(diagnostics)
                selected_vocoder = selected.vocoder
            else:
                semantic = core.GENIE().t2s_cpu(
                    cache["reference_phones"].astype(np.int64)[None, :],
                    cache["reference_bert"].T.astype(np.float32),
                    phones,
                    bert,
                    np.empty((0,), np.float32),
                    CachedEncoder(),
                    first,
                    stage,
                )
                eos = np.where(semantic >= 1024)
                if len(eos[0]):
                    semantic = semantic[..., : eos[-1][0]]
            semantic_seconds += time.monotonic() - segment_started
            segment_started = time.monotonic()
            wave = selected_vocoder.run(
                None, {"text_seq": phones, "pred_semantic": semantic, "ref_audio": audio[None, :]}
            )[0].squeeze()
            vocoder_seconds += time.monotonic() - segment_started
            outputs.append(wave)
        pcm = np.concatenate(outputs)
        elapsed = time.monotonic() - started
        peak = float(np.max(np.abs(pcm))) if len(pcm) else 0
        rms = float(np.sqrt(np.mean(pcm**2))) if len(pcm) else 0
        valid = len(pcm) > 3200 and np.isfinite(pcm).all() and peak > 0.01 and rms > 0.001
        sf.write(args.output / f"{args.variant}-{repetition}-{index}.wav", pcm, 32000, subtype="PCM_16")
        emit(
            "warmup" if repetition == -1 else "speech",
            repetition=repetition,
            index=index,
            text=text,
            seconds=elapsed,
            dynamic_frontend_seconds=frontend_seconds,
            semantic_seconds=semantic_seconds,
            vocoder_seconds=vocoder_seconds,
            segment_diagnostics=segment_diagnostics,
            audio_seconds=len(pcm) / 32000,
            rtf=elapsed / (len(pcm) / 32000) if len(pcm) else None,
            peak=peak,
            rms=rms,
            valid_non_silent=bool(valid),
        )
        if not valid:
            raise RuntimeError("Invalid/silent speech")
        if repetition == -1 and args.load_llm_after_warmup:
            if args.advise_converted_cache_before_llm:
                advise_converted_cache(args)
            emit("start_llm_load", phase="llm_load_after_genie_warmup")
            load_started = time.monotonic()
            observation = observe_mini(args.root, load=True)
            mini_identity = observation["identity"]
            emit("llm_loaded", seconds=time.monotonic() - load_started, **observation)
    if args.load_llm_after_warmup:
        if mini_identity is None:
            raise RuntimeError("Mini was not loaded after a completed explicit warmup")
        emit("llm_identity_verified", **observe_mini(args.root, expected=mini_identity))
    emit("completed", variant=args.variant)


def measured(args):
    import psutil
    from evaluate_context import MemoryWatch, ProbeCancelled

    args.output.mkdir(parents=True, exist_ok=True)
    log = args.output / f"{args.action}-{args.variant}.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--execute",
        "--child",
        "--root",
        str(args.root),
        "--source",
        str(args.source),
        "--output",
        str(args.output),
        "--action",
        args.action,
        "--variant",
        args.variant,
        "--voice",
        args.voice,
    ]
    if args.models:
        command.extend(["--models", str(args.models)])
    if args.ar_models:
        command.extend(["--ar-models", str(args.ar_models)])
    if args.bert_models:
        command.extend(["--bert-models", str(args.bert_models)])
    for name, value in configuration(args).items():
        if name == "variant" or value is None:
            continue
        option = "--" + name.replace("_", "-")
        if isinstance(value, bool):
            if value:
                command.append(option)
        else:
            command.extend([option, str(value)])
    watch = MemoryWatch(args.deadline)
    peak_rss = 0
    phases = {}
    reason = None
    with log.open("w") as stream:
        child = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while child.poll() is None:
                watch.sample()
                events = read_events(log)
                stage = events[-1]["stage"] if events else "imports"
                phases[stage] = min(
                    phases.get(stage, watch.latest["available_kib"]), watch.latest["available_kib"]
                )
                try:
                    peak_rss = max(peak_rss, psutil.Process(child.pid).memory_info().rss)
                except psutil.NoSuchProcess:
                    pass
                time.sleep(0.5)
            watch.sample()
        except (ProbeCancelled, KeyboardInterrupt) as error:
            reason = str(error)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
    events = read_events(log)
    result = {
        "action": args.action,
        "variant": args.variant,
        "options": configuration(args),
        "elapsed_seconds": time.monotonic() - watch.started,
        "completed": child.returncode == 0 and reason is None,
        "guard_reason": reason,
        "returncode": child.returncode,
        "memory": watch.summary(),
        "peak_machine_total_minus_available_kib": psutil.virtual_memory().total // 1024
        - watch.minimum_available_kib,
        "sampled_peak_child_rss_bytes": peak_rss,
        "phase_minimum_available_kib": phases,
        "events": events,
        "error_tail": log.read_text()[-5000:] if child.returncode else None,
    }
    (args.output / f"{args.action}-{args.variant}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    emit("measurement", result=result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/workspace/tuber"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--models", type=Path, help="Reuse this isolated conversion directory for a separately recorded run"
    )
    parser.add_argument("--voice", default="whale_sovits_v2")
    parser.add_argument("--action", choices=("convert", "run"), required=True)
    parser.add_argument("--variant", choices=("stock", "shared"), default="stock")
    parser.add_argument("--bert-mode", choices=("fp32", "qnnpack-int8", "onnx-int8"), default="fp32")
    parser.add_argument(
        "--bert-models", type=Path, help="Separately prepared original22-layer ONNX BERT bundle"
    )
    parser.add_argument("--sampling-mode", choices=("legacy", "reference"), default="legacy")
    parser.add_argument("--ar-mode", choices=("fp32", "int8"), default="fp32")
    parser.add_argument("--ar-models", type=Path, help="Separately prepared restored-sampling INT8 AR bundle")
    parser.add_argument(
        "--seed",
        type=int,
        help="Set process RNGs before session construction; omitted preserves the historical unseeded protocol",
    )
    parser.add_argument("--torch-threads", type=int, default=4)
    parser.add_argument("--intra-op-threads", type=int, default=4)
    parser.add_argument("--inter-op-threads", type=int, default=1)
    parser.add_argument("--cpu-arena", choices=("default", "on", "off"), default="default")
    parser.add_argument("--memory-pattern", choices=("default", "on", "off"), default="default")
    parser.add_argument("--disable-prepacking", action="store_true")
    parser.add_argument("--shared-cpu-arena", action="store_true")
    parser.add_argument(
        "--arena-extend-strategy", choices=("default", "same-as-requested"), default="default"
    )
    parser.add_argument(
        "--warmup-text",
        help="Complete fixed utterance before formal cases; its loading/time/memory remain inside the measured process",
    )
    parser.add_argument(
        "--case-order", choices=("repetition-first", "text-first"), default="repetition-first"
    )
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument(
        "--advise-converted-cache-before-llm",
        action="store_true",
        help="Advise this experiment's clean converted files only before typed LLM load; no global reclaim",
    )
    parser.add_argument(
        "--load-llm-after-warmup",
        action="store_true",
        help="After a required explicit non-silent warmup, load and verify broker Mini/native131072/q4_0; verify its PID again after speech. Experimental text-mode coexistence only",
    )
    parser.add_argument("--deadline", type=int, default=270)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.sampling_mode == "reference" and args.variant != "shared":
        parser.error("Reference sampling requires the shared, legally expanded FP32 bundle")
    if args.bert_mode == "onnx-int8" and not args.bert_models:
        parser.error("ONNX BERT requires --bert-models")
    if args.ar_mode == "int8" and (args.sampling_mode != "reference" or not args.ar_models):
        parser.error("INT8 AR requires restored reference sampling and --ar-models")
    if args.advise_converted_cache_before_llm and not args.load_llm_after_warmup:
        parser.error("Converted cache advice requires --load-llm-after-warmup")
    if args.load_llm_after_warmup and (
        args.action != "run" or not args.warmup_text or not args.warmup_text.strip()
    ):
        parser.error("--load-llm-after-warmup requires --action run and an explicit nonempty --warmup-text")
    if min(args.torch_threads, args.intra_op_threads, args.inter_op_threads, args.repetitions) < 1:
        parser.error("Thread counts and repetitions must be positive")
    if args.seed is not None and not 0 <= args.seed <= 4294967295:
        parser.error("Seed must fit an unsigned 32-bit integer")
    if args.shared_cpu_arena and args.cpu_arena == "off":
        parser.error("A shared arena cannot be combined with --cpu-arena off")
    if args.arena_extend_strategy != "default" and not args.shared_cpu_arena:
        parser.error("Arena growth strategy requires --shared-cpu-arena")
    if not args.execute:
        parser.error("Execution requires an assigned maintenance window and --execute")
    args.root, args.source, args.output = args.root.resolve(), args.source.resolve(), args.output.resolve()
    if args.models:
        args.models = args.models.resolve()
        if not args.models.is_relative_to(args.root / "runtime/evaluations"):
            parser.error("Converted models must stay in this project's isolated runtime/evaluations")
    if args.ar_models:
        args.ar_models = args.ar_models.resolve()
        if not args.ar_models.is_relative_to(args.root / "runtime/evaluations"):
            parser.error("INT8 AR bundle must stay in this project's isolated runtime/evaluations")
    if args.bert_models:
        args.bert_models = args.bert_models.resolve()
        if not args.bert_models.is_relative_to(args.root / "runtime/evaluations"):
            parser.error("ONNX BERT bundle must stay in this project's isolated runtime/evaluations")
    if not args.output.is_relative_to(args.root / "runtime/evaluations"):
        parser.error("All outputs must stay in this project's isolated runtime/evaluations")
    os.environ["TUBER_GPT_SOVITS_ROOT"] = "/opt/GPT-SoVITS-v2"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(args.root / "tools"))
    if args.child:
        # Upstream conversion removes its cwd/Cache on exit: own that directory.
        os.chdir(args.output)
        (convert if args.action == "convert" else run)(args)
    else:
        measured(args)


if __name__ == "__main__":
    main()
