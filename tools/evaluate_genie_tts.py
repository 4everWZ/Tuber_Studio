"""Isolated Genie v2 CPU backend with original dynamic Chinese frontend.

No dependency installs, production configuration writes, or broker lifecycle
changes happen here. The maintenance controller owns the exclusive window and
Mini restoration. Genie templates/converters are MIT High_Logic (2025); stock
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


def emit(stage, **fields):
    print(PREFIX + json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


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

    torch.set_num_threads(4)
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
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    upstream = compact._upstream(args.root)
    base = args.root / "runtime/tts/pretrained/chinese-roberta-wwm-ext-large"
    masked = AutoModelForMaskedLM.from_pretrained(base, torch_dtype=torch.float32, local_files_only=True)
    if masked.config.model_type != "bert" or len(masked.bert.encoder.layer) != 24:
        raise RuntimeError("The original 24-layer BERT checkpoint is required")
    backbone = masked.bert
    backbone.encoder.layer = torch.nn.ModuleList(list(backbone.encoder.layer)[:22])
    backbone.config.num_hidden_layers = 22
    tokenizer = AutoTokenizer.from_pretrained(base, local_files_only=True)
    pre = upstream.TextPreprocessor(backbone.eval(), tokenizer, torch.device("cpu"))
    del masked

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

    torch.set_num_threads(4)
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
    emit("bert_loaded", seconds=time.monotonic() - load_started, layers=22, dtype="float32", device="cpu")
    stock = stock_function(args.source)
    modeldir = args.models or args.output / "models"
    held = {}

    def session(path, binname=None):
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        emit("session_loading", file=path.name, variant=args.variant)
        if binname is None:
            return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=options)
        binpath = modeldir / binname
        if args.variant == "stock":
            return stock(str(path), str(binpath), ["CPUExecutionProvider"], options)
        # One immutable FP32 buffer per bin, shared across the two AR sessions.
        # Keep arrays/OrtValues alive for the complete session lifetime.
        if binname not in held:
            expanded = np.fromfile(binpath, dtype=np.float16).astype(np.float32)
            external_path = modeldir / binname.replace("fp16", "fp32")
            expanded.tofile(external_path)
            del expanded
            held[binname] = {"array": np.memmap(external_path, mode="r", dtype=np.float32), "values": []}
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

    encoder = session(pruned_encoder(args))
    vocoder = session(modeldir / "vits_fp32.onnx", "vits_fp16.bin")
    first = session(modeldir / "t2s_first_stage_decoder_fp32.onnx", "t2s_shared_fp16.bin")
    stage = session(modeldir / "t2s_stage_decoder_fp32.onnx", "t2s_shared_fp16.bin")
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

    for repetition in range(2):
        for index, text in enumerate(TEXTS):
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
            for item in items:
                phones = np.asarray([item["phones"]], dtype=np.int64)
                bert = item["bert_features"].T.contiguous().numpy().astype(np.float32)
                if not np.any(bert) or not np.isfinite(bert).all():
                    raise RuntimeError("Chinese BERT features are zero/nonfinite")
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
                wave = vocoder.run(
                    None, {"text_seq": phones, "pred_semantic": semantic, "ref_audio": audio[None, :]}
                )[0].squeeze()
                outputs.append(wave)
            pcm = np.concatenate(outputs)
            elapsed = time.monotonic() - started
            peak = float(np.max(np.abs(pcm))) if len(pcm) else 0
            rms = float(np.sqrt(np.mean(pcm**2))) if len(pcm) else 0
            valid = len(pcm) > 3200 and np.isfinite(pcm).all() and peak > 0.01 and rms > 0.001
            sf.write(args.output / f"{args.variant}-{repetition}-{index}.wav", pcm, 32000, subtype="PCM_16")
            emit(
                "speech",
                repetition=repetition,
                index=index,
                text=text,
                seconds=elapsed,
                dynamic_frontend_seconds=frontend_seconds,
                audio_seconds=len(pcm) / 32000,
                rtf=elapsed / (len(pcm) / 32000) if len(pcm) else None,
                peak=peak,
                rms=rms,
                valid_non_silent=bool(valid),
            )
            if not valid:
                raise RuntimeError("Invalid/silent speech")
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
    parser.add_argument("--deadline", type=int, default=270)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.execute:
        parser.error("Execution requires an assigned maintenance window and --execute")
    args.root, args.source, args.output = args.root.resolve(), args.source.resolve(), args.output.resolve()
    if args.models:
        args.models = args.models.resolve()
        if not args.models.is_relative_to(args.root / "runtime/evaluations"):
            parser.error("Converted models must stay in this project's isolated runtime/evaluations")
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
