"""Isolated v2 TTS candidates; inspect does not import Torch or probe CUDA.

Run inspect in the existing project interpreter first. Dependencies belong in a
separate runtime evaluation venv that reuses /opt/venv's Torch. This tool never
installs packages or changes a production profile. In tts-first mode it requests
typed Mini warmup after TTS is ready; the maintenance controller owns unloading
the original Mini beforehand and restoring it after the candidate exits.
Actual execution requires --execute and an explicit GPU ownership handoff.
CPU BERT INT8 changes numerics; Lite also changes the text frontend. Neither is
an output-equivalent replacement until voice/quality comparisons are completed.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata as metadata
import json
import os
import platform
import signal
import subprocess
import sys
import time
from pathlib import Path

PREFIX = "TUBER_TTS_CANDIDATE "
LITE_VERSION = "0.4.7"
LITE_REVISION = "6c049397142f4c9147a85f86b6ba37546e93a188"
OFFICIAL_REVISION = "b4570b876f5c086819ba84e3215472733c6a7b70"
WARMUP_TEXT = "海底厨房开门啦，今天的饭团要趁热吃。"
V2_SAMPLING = {"top_k": 5, "top_p": 1.0, "temperature": 1.0, "repetition_penalty": 1.35}
WHEEL_PLAN = (
    (
        "gsv-tts-lite",
        "0.4.7",
        "gsv_tts_lite-0.4.7-py3-none-any.whl",
        "70b8101bae0d53367fe750c75571a5eff43b97a455964eaacda1879f06a50b6a",
    ),
    (
        "av",
        "16.0.1",
        "av-16.0.1-cp312-cp312-manylinux_2_28_aarch64.whl",
        "5201f7b4b5ed2128118cb90c2a6d64feedb0586ca7c783176896c78ffb4bbd5c",
    ),
    (
        "sounddevice",
        "0.5.2",
        "sounddevice-0.5.2-py3-none-any.whl",
        "82375859fac2e73295a4ab3fc60bd4782743157adc339561c1f1142af472f505",
    ),
    (
        "pysbd",
        "0.3.4",
        "pysbd-0.3.4-py3-none-any.whl",
        "cd838939b7b0b185fcf86b0baf6636667dfb6e474743beeff878e9f42e022953",
    ),
    (
        "onnxruntime",
        "1.22.1",
        "onnxruntime-1.22.1-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl",
        "984cea2a02fcc5dfea44ade9aca9fe0f7a8a2cd6f77c258fc4388238618f3928",
    ),
    (
        "pyopenjtalk-plus",
        "0.4.1.post7",
        "pyopenjtalk_plus-0.4.1.post7-cp312-cp312-manylinux_2_24_aarch64.manylinux_2_28_aarch64.whl",
        "91a04f0c5fde631ca0cf65277b42d379c52fdbf56d0eb6bccafa0847a0cabcae",
    ),
)
PACKAGES = (
    "torch",
    "torchaudio",
    "torchvision",
    "transformers",
    "torchao",
    "gsv-tts-lite",
    "numpy",
    "PyYAML",
    "httpx",
    "psutil",
    "av",
    "sounddevice",
    "pysbd",
    "onnxruntime",
    "pyopenjtalk-plus",
    "pyopenjtalk",
    "safetensors",
)


def emit(stage, **fields):
    print(PREFIX + json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def package_info(name):
    try:
        distribution = metadata.distribution(name)
        return {"version": distribution.version, "location": str(distribution.locate_file(""))}
    except metadata.PackageNotFoundError:
        return None


def source_check(path, functions=(), classes=()):
    """Read syntax/API availability without importing a CUDA-active module."""
    if not path.is_file():
        return {"exists": False, "path": str(path)}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        definitions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        class_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
        return {
            "exists": True,
            "path": str(path),
            "syntax_valid": True,
            "functions": {name: name in definitions for name in functions},
            "classes": {name: name in class_names for name in classes},
        }
    except (SyntaxError, UnicodeError) as error:
        return {"exists": True, "path": str(path), "syntax_valid": False, "error": str(error)}


def inspect_environment(args):
    packages = {name: package_info(name) for name in PACKAGES}
    torchao = packages["torchao"]
    quant_api = None
    if torchao:
        quant_api = source_check(
            Path(torchao["location"]) / "torchao/quantization/quant_api.py",
            classes=("Int8WeightOnlyConfig",),
        )
    lite = packages["gsv-tts-lite"]
    lite_source = None
    if lite:
        lite_source = source_check(
            Path(lite["location"]) / "gsv_tts/TTS.py",
            functions=("load_gpt_model", "load_sovits_model", "cache_prompt_audio", "infer"),
        )
    upstream = Path(os.environ.get("TUBER_GPT_SOVITS_ROOT", "/opt/GPT-SoVITS-v2"))
    official = args.official_source
    return {
        "action": "inspect",
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "architecture": platform.machine(),
        "packages": packages,
        "torch_imported": "torch" in sys.modules,
        "cuda_probe_performed": False,
        "speech_corpus": {
            "source": str(args.speech_cases) if args.speech_cases else "default 3-case screening",
            "planned_count": len(speech_texts(args)) if args.speech_cases else 3,
            "executed_count": 0,
        },
        "current_v2": source_check(upstream / "GPT_SoVITS/TTS_infer_pack/TTS.py"),
        "reference_cache_present": all(
            (args.root / "runtime/tts/reference_features" / args.voice / name).is_file()
            for name in ("metadata.json", "features.npz")
        ),
        "torchao_quantization_api": quant_api,
        "lite_source": lite_source,
        "official_ar_source": None
        if official is None
        else source_check(official / "GPT_SoVITS/Accel/PyTorch/AR/t2s_model_abc.py", functions=("quantize",)),
        "candidate_requirements": {
            "bert-cuda-int8wo": {
                "additional_python_packages": [] if torchao else ["Compatible pinned TorchAO"],
                "gates": [
                    "Existing TorchAO 0.10 weight-only CUDA dispatch casts INT8 weights to the FP16 input dtype before torch.mm.",
                    "Weight-only storage and integer GEMM are separate claims; the executed dispatch must be recorded.",
                    "Per-channel Int8WeightOnlyConfig is the minimal BERT probe; official AR's group-size 32 has different dispatch eligibility.",
                    "Dynamic activation INT8 is a separate micro-probe, not a silent change to the weight-only candidate.",
                ],
            },
            "bert-int8": {
                "additional_python_packages": [],
                "gates": [
                    "CPU quantized engine must execute quantized::linear_dynamic, not just serialize INT8 weights.",
                    "Keep the existing 22-layer BERT representation and FP16 AR/SoVITS backend.",
                    "Use a current fixed-reference compact cache; new Chinese text is still processed dynamically.",
                    "CPU residency uses Jetson shared DRAM; measure cold peak and synthesis latency.",
                ],
            },
            "lite": {
                "version": LITE_VERSION,
                "source_revision": LITE_REVISION,
                "missing_distributions": [
                    name
                    for name in (
                        "gsv-tts-lite",
                        "av",
                        "sounddevice",
                        "pysbd",
                        "onnxruntime",
                        "pyopenjtalk-plus",
                    )
                    if packages[name] is None
                ],
                "preprovisioned_model_directories": [
                    "chinese-hubert-base",
                    "chinese-roberta-wwm-ext-large",
                    "g2p",
                    "sv",
                ],
                "gates": [
                    "Importing gsv_tts.Config can initialize CUDA and run attention probes; do not import during inspect.",
                    "Explicit use_bert=True, original GPT/SoVITS checkpoints and original reference audio/text.",
                    "No automatic asset download or default newer voice checkpoint during measurement.",
                    "pyopenjtalk-plus shares an import namespace with pyopenjtalk; keep it isolated from production.",
                    "sounddevice may require libportaudio; av/onnxruntime need compatible aarch64/Python wheels.",
                    "Changed Chinese frontend is not an output-equivalence claim.",
                ],
            },
            "official-ar-int8": {
                "source_revision": OFFICIAL_REVISION,
                "source_url": "https://github.com/RVC-Boss/GPT-SoVITS/tree/" + OFFICIAL_REVISION,
                "gates": [
                    "Current 20240821v2 scripted AR consumes plain qkv/MLP tensors; quantizing registered nn.Linear alone does not establish INT8 execution.",
                    "Official Accel is a separate decoder implementation, absent from the old upstream source.",
                    "Ordinary AccelInference does not expose quantize_mode; the decoder loader must receive it explicitly.",
                    "Validate existing TorchAO's Int8WeightOnlyConfig and real execution; do not upgrade original Torch.",
                    "This tool does not yet replace the AR decoder or claim an end-to-end official-AR INT8 candidate.",
                ],
            },
        },
        "dependency_policy": {
            "installation_performed": False,
            "location": "project runtime evaluation venv/directory only",
            "reuse_torch_from": str(args.base_torch_root),
            "instructions": [
                "Create an evaluation venv with access to the current project/base site-packages.",
                "Install the pinned candidate and reviewed missing dependencies only into that venv; constrain/reuse the existing torch, torchaudio and torchvision.",
                "Use --no-deps for the candidate itself; review dependency wheels/build requirements before installing the missing list.",
                "Provision Lite frontend/reference assets separately before any timed run; inspect does not download anything.",
            ],
            "wheel_plan": [
                {
                    "distribution": name,
                    "version": version,
                    "filename": filename,
                    "sha256": digest,
                    "source": f"https://pypi.org/pypi/{name}/{version}/json",
                    "downloaded_by_this_tool": False,
                }
                for name, version, filename, digest in WHEEL_PLAN
            ],
            "remaining_transitive_gates": [
                "sounddevice requires CFFI and libportaudio; do not install an OS package globally for this experiment.",
                "ONNX Runtime requires compatible coloredlogs/flatbuffers/protobuf and other metadata dependencies.",
                "pyopenjtalk-plus requires SudachiPy and a dictionary; preserve the original production frontend.",
                "Wheel filenames/hashes were reviewed on 2026-10-29; nothing is downloaded or installed by inspect.",
                "Lite's own g2p and SV asset pack must be inventoried separately; do not download a complete training/Torch bundle.",
            ],
        },
        "claim": "Metadata and source syntax only; no model forward, integer operator or GPU execution verified.",
    }


def require_original_torch(args):
    info = package_info("torch")
    if not info or info["version"].split("+")[0] not in {"2.7.0", "2.7.1"}:
        raise RuntimeError("This experiment requires the existing Torch 2.7 runtime")
    if not Path(info["location"]).resolve().is_relative_to(args.base_torch_root.resolve()):
        raise RuntimeError("Torch was shadowed outside the original base environment")


def int8_operator(engine):
    """Small CPU gate; success requires an executed quantized operator."""
    import torch

    supported = torch.backends.quantized.supported_engines
    if engine not in supported or engine == "none":
        raise RuntimeError(f"CPU quantized engine {engine!r} unavailable: {supported}")
    torch.backends.quantized.engine = engine
    torch.set_num_threads(2)
    layer = torch.nn.Sequential(torch.nn.Linear(32, 16)).eval()
    quantized = torch.ao.quantization.quantize_dynamic(layer, {torch.nn.Linear}, dtype=torch.qint8)
    with (
        torch.inference_mode(),
        torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU]) as trace,
    ):
        output = quantized(torch.ones(1, 32))
    operators = {
        item.key: item.count for item in trace.key_averages() if "quantized::linear_dynamic" in item.key
    }
    if not operators or not torch.isfinite(output).all():
        raise RuntimeError("No finite result from a genuinely executed quantized linear operator")
    return {"engine": engine, "operators": operators, "weight_dtype": str(quantized[0].weight().dtype)}


def quantized_storage(model):
    """Inspect the actual packed tensors rather than the wrapper's logical dtype."""
    weights = []
    for name, module in model.named_modules():
        weight = getattr(module, "weight", None)
        implementation = getattr(weight, "tensor_impl", None)
        if implementation is None or not callable(getattr(implementation, "get_plain", None)):
            continue
        integer, scale, zero = implementation.get_plain()
        weights.append(
            {
                "name": name,
                "wrapper": type(weight).__name__,
                "logical_dtype": str(weight.dtype),
                "storage_dtype": str(integer.dtype),
                "storage_bytes": integer.numel() * integer.element_size(),
                "scale_bytes": scale.numel() * scale.element_size(),
                "zero_point_bytes": 0 if zero is None else zero.numel() * zero.element_size(),
                "device": str(integer.device),
            }
        )
    return {
        "linear_weights": weights,
        "int8_storage_observed": any(item["storage_dtype"] == "torch.int8" for item in weights),
    }


def profiled_forward(model, inputs):
    import torch
    from torch.utils._python_dispatch import TorchDispatchMode

    dispatched = {}

    class ObserveMatmul(TorchDispatchMode):
        def __torch_dispatch__(self, function, types, args=(), kwargs=None):
            name = str(function)
            if any(part in name for part in ("mm.", "matmul.", "linear_dynamic")):
                dtypes = [str(arg.dtype) for arg in args if isinstance(arg, torch.Tensor)]
                key = name + ":" + ",".join(dtypes)
                dispatched[key] = dispatched.get(key, 0) + 1
            return function(*args, **(kwargs or {}))

    with (
        torch.inference_mode(),
        torch.profiler.profile(activities=[torch.profiler.ProfilerActivity.CPU]) as trace,
        ObserveMatmul(),
    ):
        output = model(**inputs) if isinstance(inputs, dict) else model(inputs)
    operators = {item.key: item.count for item in trace.key_averages()}
    integer_operators = {
        name: count
        for name, count in operators.items()
        if any(part in name for part in ("quantized::linear_dynamic", "aten::_int_mm", "int_scaled_matmul"))
    }
    return output, {
        "integer_compute_observed": bool(integer_operators),
        "integer_operators": integer_operators,
        "matmul_dispatch": dispatched,
        "classification": "integer_compute" if integer_operators else "no_integer_compute_observed",
    }


def cuda_operator(quantization):
    import torch
    from torchao.quantization import quantize_
    from torchao.quantization.quant_api import Int8WeightOnlyConfig

    configuration = Int8WeightOnlyConfig()
    if quantization == "dynamic-activation":
        from torchao.quantization.quant_api import Int8DynamicActivationInt8WeightConfig

        configuration = Int8DynamicActivationInt8WeightConfig()
    layer = torch.nn.Sequential(torch.nn.Linear(1024, 1024)).eval().to("cuda", torch.float16)
    quantize_(layer, configuration)
    storage = quantized_storage(layer)
    output, evidence = profiled_forward(layer, torch.ones(1, 32, 1024, device="cuda", dtype=torch.float16))
    torch.cuda.synchronize()
    if not storage["int8_storage_observed"] or not torch.isfinite(output).all():
        raise RuntimeError("Quantized CUDA linear did not execute with finite output and real INT8 storage")
    return {
        "quantization": quantization,
        "input_dtype": "torch.float16",
        "output_dtype": str(output.dtype),
        "storage": storage,
        "execution": evidence,
        "genuine_integer_compute_passed": evidence["integer_compute_observed"],
        "warning": None
        if evidence["integer_compute_observed"]
        else "INT8 weight storage with floating GEMM is not an INT8-compute result.",
    }


def fixed_settings(args):
    import yaml

    voice = next(
        item
        for item in json.loads((args.root / "config/voices.json").read_text())
        if item["id"] == args.voice and item["provider"] == "gpt_sovits"
    )
    config = yaml.safe_load((args.root / "runtime/tts/tts_infer.yaml").read_text())["custom"]
    if config["version"] != "v2" or not config["is_half"] or config["device"] != "cuda":
        raise RuntimeError("Requires the existing v2/CUDA/FP16 profile")
    return voice, config


def speech_texts(args):
    if args.speech_cases is None:
        from evaluate_tts_memory import TEXTS

        return list(TEXTS)
    data = json.loads(args.speech_cases.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        if data.get("voice_id", args.voice) != args.voice or data.get("seed", 42) != 42:
            raise ValueError("Speech corpus voice/seed differs from the selected fixed voice and seed42")
        data = data.get("texts")
    if (
        not isinstance(data, list)
        or not data
        or any(not isinstance(text, str) or not text.strip() for text in data)
    ):
        raise ValueError("Speech cases must contain an ordered nonempty texts array")
    return data


def diagnostic_identity(args):
    cache = args.root / "runtime/tts/reference_features" / args.voice / "metadata.json"
    return {
        "candidate": args.candidate,
        "voice": args.voice,
        "warmup_text": args.warmup_text,
        "packages": {name: package_info(name) for name in ("torch", "torchao", "transformers")},
        "reference_identity": json.loads(cache.read_text(encoding="utf-8"))["identity"],
    }


def validated_diagnostic(args):
    if args.action != "run" or args.candidate not in {"bert-int8", "bert-cuda-int8wo"}:
        return None
    if args.diagnostic_result is None:
        raise RuntimeError(
            "Run a separate --action diagnose first; formal measurement needs --diagnostic-result"
        )
    result = json.loads(args.diagnostic_result.read_text(encoding="utf-8"))
    if (
        not result.get("completed")
        or result.get("measurement_kind") != "instrumented_diagnostic"
        or result.get("diagnostic_identity") != diagnostic_identity(args)
    ):
        raise RuntimeError(
            "Diagnostic did not complete with the same candidate, reference and package identity"
        )
    if args.candidate == "bert-int8" and not result.get("genuine_integer_compute_passed"):
        raise RuntimeError("CPU INT8 diagnostic has no executed integer-operator evidence")
    return result


def bert_int8_pipeline(args):
    """Keep compact's checked source cache and synthesis path; quantize BERT only."""
    import types

    import torch
    from jetson.tts import compact

    root, directory, config, identity = compact._settings(args.root, args.voice)
    cached, arrays = compact._read_bundle(directory, identity)
    signature = compact.reference_signature(root, voice_id=args.voice, metadata=cached)
    upstream = compact._upstream(root)

    cuda_weight_only = args.candidate == "bert-cuda-int8wo"

    class BertInt8TTS(upstream.TTS):
        def init_cnhuhbert_weights(self, base_path):
            self.cnhuhbert_model = None

        def init_bert_weights(self, base_path):
            from transformers import AutoModelForMaskedLM, AutoTokenizer

            # Start with the intended precision, avoiding a redundant GPU BERT.
            masked = AutoModelForMaskedLM.from_pretrained(
                base_path,
                torch_dtype=torch.float16 if cuda_weight_only else torch.float32,
                local_files_only=True,
            )
            if masked.config.model_type != "bert" or len(masked.bert.encoder.layer) != 24:
                raise RuntimeError("Expected the original 24-layer BERT checkpoint")
            backbone = masked.bert
            backbone.encoder.layer = torch.nn.ModuleList(list(backbone.encoder.layer)[:22])
            backbone.config.num_hidden_layers = 22
            self.bert_tokenizer = AutoTokenizer.from_pretrained(base_path, local_files_only=True)
            if cuda_weight_only:
                from torchao.quantization import Int8WeightOnlyConfig, quantize_

                self.bert_model = backbone.eval().to("cuda")
                quantize_(self.bert_model, Int8WeightOnlyConfig())
            else:
                self.bert_model = torch.ao.quantization.quantize_dynamic(
                    backbone.eval(), {torch.nn.Linear}, dtype=torch.qint8, inplace=True
                )
            del masked

        def set_ref_audio(self, path):
            if (
                str(compact._file(root, path, ("runtime/tts/ref", "assets")))
                != identity["paths"]["reference"]
            ):
                raise RuntimeError("Reference changed during candidate evaluation")

        def run(self, inputs):
            if compact.reference_signature(root, voice_id=args.voice, metadata=cached) != signature:
                raise RuntimeError("Voice assets changed during candidate evaluation")
            self.set_ref_audio(inputs.get("ref_audio_path", ""))
            if (
                inputs.get("aux_ref_audio_paths")
                or inputs.get("prompt_lang") != identity["prompt_lang"]
                or compact._normal_prompt(inputs.get("prompt_text", ""), inputs.get("prompt_lang"))
                != identity["prompt_text"]
            ):
                raise RuntimeError("Reference transcript/language changed")
            yield from super().run(inputs)

    private_config = args.output / "bert-int8-config.yaml"
    private_config.write_bytes(config.read_bytes())
    pipeline = BertInt8TTS(str(private_config))
    pipeline.prompt_cache.update(
        ref_audio_path=identity["paths"]["reference"],
        prompt_semantic=torch.from_numpy(arrays["prompt_semantic"]).to("cuda"),
        refer_spec=[torch.from_numpy(arrays["reference_spec"]).to("cuda")],
        prompt_text=identity["prompt_text"],
        prompt_lang=identity["prompt_lang"],
        phones=arrays["reference_phones"].tolist(),
        bert_features=torch.from_numpy(arrays["reference_bert"]).to("cuda"),
        norm_text=cached["norm_text"],
        aux_ref_audio_paths=[],
    )
    pipeline.int8_operators = {}
    pipeline.integer_compute_observed = False
    if cuda_weight_only:
        storage = quantized_storage(pipeline.bert_model)
        if not storage["int8_storage_observed"]:
            raise RuntimeError("BERT was not converted to genuine INT8 weight storage")
        emit("bert_storage", result=storage)

    def feature(preprocessor, text, word2ph):
        inputs = preprocessor.tokenizer(text, return_tensors="pt")
        inputs = {key: value.to("cuda" if cuda_weight_only else "cpu") for key, value in inputs.items()}
        inputs.update(output_hidden_states=False, return_dict=True)
        if args.action == "diagnose" and not pipeline.int8_operators and not pipeline.dispatch_diagnosed:
            result, evidence = profiled_forward(preprocessor.bert_model, inputs)
            if not cuda_weight_only and not evidence["integer_compute_observed"]:
                raise RuntimeError("BERT forward did not execute an INT8 operator")
            pipeline.int8_operators.update(evidence["integer_operators"])
            pipeline.integer_compute_observed |= evidence["integer_compute_observed"]
            pipeline.dispatch_diagnosed = True
            emit("bert_dispatch", result=evidence)
        else:
            # No profiler or Python dispatch instrumentation in timed/memory measurements.
            with torch.inference_mode():
                result = preprocessor.bert_model(**inputs)
        hidden = result.last_hidden_state[0].cpu()[1:-1]
        if len(word2ph) != len(text) or not torch.isfinite(hidden).all():
            raise RuntimeError("Invalid CPU INT8 Chinese BERT features")
        return torch.cat([hidden[i].repeat(word2ph[i], 1) for i in range(len(word2ph))]).T.half()

    pipeline.text_preprocessor.get_bert_feature = types.MethodType(feature, pipeline.text_preprocessor)
    pipeline.dispatch_diagnosed = False
    if compact.reference_signature(root, voice_id=args.voice, metadata=cached) != signature:
        raise RuntimeError("Voice assets changed while loading candidate")
    return pipeline


def lite_pipeline(args, config):
    installed = package_info("gsv-tts-lite")
    if not installed or installed["version"] != LITE_VERSION:
        raise RuntimeError("Only reviewed GSV-TTS-Lite 0.4.7 is supported")
    if args.lite_models is None:
        raise RuntimeError("Provide a separately provisioned --lite-models directory")
    for name in ("chinese-hubert-base", "chinese-roberta-wwm-ext-large", "g2p", "sv"):
        if not (args.lite_models / name).is_dir():
            raise RuntimeError("Missing offline Lite asset directory: " + name)
    import requests

    def no_download(*unused_args, **unused_kwargs):
        raise RuntimeError("Asset download is disabled inside a measured candidate run")

    requests.sessions.Session.request = no_download
    from gsv_tts import TTS

    pipeline = TTS(
        models_dir=str(args.lite_models),
        device="cuda",
        dtype="float16",
        use_bert=True,
        use_flash_attn=False,
        gpt_cache=[(1, 1024)],
        sovits_cache=[],
        always_load_cnhubert=False,
        always_load_sv=False,
    )
    pipeline.load_gpt_model(config["t2s_weights_path"])
    pipeline.load_sovits_model(config["vits_weights_path"])
    voice, _ = fixed_settings(args)
    emit("start_reference_precompute")
    pipeline.cache_prompt_audio(
        prompt_audio_paths=voice["server_reference_path"],
        prompt_audio_texts=voice["reference_text"],
        prompt_language=voice["params"]["prompt_lang"],
    )
    pipeline.cache_spk_audio(voice["server_reference_path"], sovits_model=config["vits_weights_path"])
    if pipeline.cnhubert_model is not None or pipeline.sv_model is not None:
        raise RuntimeError("Lite reference encoders remained resident before LLM warmup")
    if voice["server_reference_path"] not in pipeline.prompt_audio_cache:
        raise RuntimeError("Lite did not cache the configured original reference")
    emit("reference_precomputed", cnhubert_released=True, sv_released=True)
    return pipeline


def child(args):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import hashlib
    import random
    import wave

    import numpy as np
    import torch
    from jetson.tts import compact

    require_original_torch(args)
    if args.action in {"int8-operator", "cuda-int8-operator"}:
        result = (
            int8_operator(args.bert_engine)
            if args.action == "int8-operator"
            else cuda_operator(args.cuda_quantization)
        )
        emit("operator_diagnostic", result=result)
        emit("completed", genuine_integer_compute_passed=result.get("genuine_integer_compute_passed", True))
        return
    voice, config = fixed_settings(args)
    texts = speech_texts(args)
    sampling = {name: voice["params"].get(name, value) for name, value in V2_SAMPLING.items()}
    torch.set_num_threads(2)
    torch.manual_seed(42)
    if args.candidate == "bert-int8" and args.action == "diagnose":
        emit("integer_operator_gate", result=int8_operator(args.bert_engine))
    elif args.candidate == "bert-cuda-int8wo" and args.action == "diagnose":
        emit("integer_operator_gate", result=cuda_operator("weight-only"))
    elif args.candidate == "bert-int8":
        if args.bert_engine not in torch.backends.quantized.supported_engines:
            raise RuntimeError("Previously diagnosed CPU quantized engine is no longer available")
        torch.backends.quantized.engine = args.bert_engine
    emit("loading", candidate=args.candidate)
    started = time.monotonic()
    pipeline = (
        lite_pipeline(args, config)
        if args.candidate == "lite"
        else bert_int8_pipeline(args)
        if args.candidate in {"bert-int8", "bert-cuda-int8wo"}
        else compact.create_pipeline(args.root, voice_id=args.voice)
    )
    torch.cuda.synchronize()
    emit("loaded", seconds=time.monotonic() - started)

    def synthesize(text, index, phase):
        emit("start_" + phase, index=index)
        random.seed(42)
        np.random.seed(42)
        torch.manual_seed(42)
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        if args.candidate == "lite":
            clip = pipeline.infer(
                spk_audio_path=voice["server_reference_path"],
                prompt_audio_path=voice["server_reference_path"],
                prompt_audio_text=voice["reference_text"],
                text=text,
                text_language=voice["params"]["text_lang"],
                prompt_language=voice["params"]["prompt_lang"],
                gpt_model=config["t2s_weights_path"],
                sovits_model=config["vits_weights_path"],
                speed=voice["params"].get("speed_factor", 1.0),
                noise_scale=0.5,
                **sampling,
            )
            raw = np.asarray(clip.audio_data)
            if raw.ndim != 1 or not np.issubdtype(raw.dtype, np.floating) or not np.isfinite(raw).all():
                raise RuntimeError("Invalid Lite waveform")
            pcm, rate = (np.clip(raw, -1, 1) * 32767).astype(np.int16), clip.samplerate
        else:
            inputs = {
                "text": text,
                "ref_audio_path": voice["server_reference_path"],
                "prompt_text": voice["reference_text"],
                "seed": 42,
                "batch_size": 1,
                **sampling,
                **voice["params"],
            }
            chunks = list(pipeline.run(inputs))
            if not chunks or len({rate for rate, _ in chunks}) != 1:
                raise RuntimeError("Missing/inconsistent PCM chunks")
            rate = chunks[0][0]
            pcm = np.concatenate([chunk for _, chunk in chunks])
        torch.cuda.synchronize()
        elapsed = time.monotonic() - started
        if pcm.dtype != np.int16 or pcm.ndim != 1 or pcm.size == 0 or not pcm.any():
            raise RuntimeError("Empty or silent synthesis is not success")
        path = args.output / f"{args.candidate}-{phase}-{index}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes(pcm.tobytes())
        emit(
            phase,
            index=index,
            text=text,
            seconds=elapsed,
            duration_seconds=pcm.size / rate,
            sample_rate=rate,
            rms_pcm16=float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))),
            pcm_sha256=hashlib.sha256(pcm.tobytes()).hexdigest(),
            cuda_peak_allocated_bytes=torch.cuda.max_memory_allocated(),
            cuda_peak_reserved_bytes=torch.cuda.max_memory_reserved(),
            int8_operators=getattr(pipeline, "int8_operators", None),
            sampling=sampling,
            seed=42,
            speed_factor=voice["params"].get("speed_factor", 1.0),
            vits_noise_scale=0.5,
        )

    # Cold startup includes a complete utterance, frontend initialization and allocator warmup.
    # In tts-first mode this finishes before the broker is asked to load Mini.
    synthesize(args.warmup_text, 0, "cold_warmup")
    if args.candidate == "lite" and (pipeline.cnhubert_model is not None or pipeline.sv_model is not None):
        raise RuntimeError("Lite reference encoder became resident during cold warmup")
    emit(
        "ready_before_llm",
        warmup_text=args.warmup_text,
        candidate_loading_and_warmup_seconds=time.monotonic() - started,
        reference_encoders_released=True if args.candidate == "lite" else None,
    )
    if args.load_order == "tts-first":
        import httpx
        from evaluate_tts_memory import profile

        emit("start_llm_load")
        headers = {"X-Tuber-Inference-Key": (args.root / "runtime/inference-token").read_text().strip()}
        response = httpx.post(
            "http://127.0.0.1:9012/llm/2b/warmup", headers=headers, json={}, trust_env=False, timeout=180
        )
        response.raise_for_status()
        emit("llm_loaded", profile=profile(args.root))
    for index, text in enumerate(texts[:1] if args.action == "diagnose" else texts):
        synthesize(text, index, "speech")
    if args.candidate == "bert-int8" and args.action == "diagnose" and not pipeline.int8_operators:
        raise RuntimeError("No integer BERT execution evidence")
    emit(
        "completed",
        candidate=args.candidate,
        voice_quality_verified=False,
        speech_case_count=1 if args.action == "diagnose" else len(texts),
        genuine_integer_compute_passed=getattr(pipeline, "integer_compute_observed", None)
        if args.action == "diagnose"
        else None,
    )


def measured(args):
    import psutil
    from evaluate_context import MemoryWatch, ProbeCancelled
    from evaluate_tts_memory import profile

    before = profile(args.root, args.load_order != "llm-first")
    prior_diagnostic = validated_diagnostic(args)
    log = args.output / "child.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--action",
        args.action,
        "--execute",
        "--child",
        "--candidate",
        args.candidate,
        "--root",
        str(args.root),
        "--output",
        str(args.output),
        "--base-torch-root",
        str(args.base_torch_root),
        "--bert-engine",
        args.bert_engine,
        "--voice",
        args.voice,
        "--load-order",
        args.load_order,
        "--cuda-quantization",
        args.cuda_quantization,
        "--warmup-text",
        args.warmup_text,
    ]
    if args.lite_models:
        command.extend(["--lite-models", str(args.lite_models)])
    if args.speech_cases:
        command.extend(["--speech-cases", str(args.speech_cases)])
    watch, reason, peak_rss = MemoryWatch(args.deadline), None, 0
    phases, stage, offset = {}, "python_import", 0
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            next_progress = time.monotonic() + 5
            while process.poll() is None:
                watch.sample()
                with log.open("rb") as progress:
                    progress.seek(offset)
                    while line := progress.readline():
                        if not line.endswith(b"\n"):
                            break
                        offset = progress.tell()
                        if line.startswith(PREFIX.encode()):
                            event = json.loads(line[len(PREFIX) :].decode("utf-8"))
                            if event["stage"] in {
                                "loading",
                                "loaded",
                                "start_reference_precompute",
                                "reference_precomputed",
                                "start_cold_warmup",
                                "ready_before_llm",
                                "start_llm_load",
                                "llm_loaded",
                                "start_speech",
                                "operator_diagnostic",
                            }:
                                stage = event["stage"]
                                if stage == "start_speech":
                                    stage += "_" + str(event["index"])
                phase = phases.setdefault(stage, {"minimum_available_kib": watch.latest["available_kib"]})
                phase["minimum_available_kib"] = min(
                    phase["minimum_available_kib"], watch.latest["available_kib"]
                )
                try:
                    peak_rss = max(peak_rss, psutil.Process(process.pid).memory_info().rss)
                except psutil.NoSuchProcess:
                    pass
                if time.monotonic() >= next_progress:
                    emit("progress", candidate=args.candidate, memory=watch.latest)
                    next_progress = time.monotonic() + 5
                time.sleep(0.5)
            watch.sample()
        except (ProbeCancelled, KeyboardInterrupt) as error:
            reason = str(error) or "Interrupted"
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
    events = [
        json.loads(line[len(PREFIX) :])
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith(PREFIX)
    ]
    try:
        after = profile(args.root, args.load_order == "isolated")
    except Exception as error:
        after = {"verification_error": str(error)}
    loaded_profile = next((e["profile"] for e in events if e["stage"] == "llm_loaded"), before)
    unchanged = loaded_profile == after and all(
        before.get(key) == loaded_profile.get(key) for key in ("model", "context_when_resident", "kv")
    )
    if args.load_order == "tts-first" and not any(e["stage"] == "llm_loaded" for e in events):
        unchanged = False
    total_kib = psutil.virtual_memory().total // 1024
    instrumented = args.action != "run"
    corpus_count = len(speech_texts(args)) if args.action in {"run", "diagnose"} else 0
    scheduled_count = min(1, corpus_count) if args.action == "diagnose" else corpus_count
    result = {
        "candidate": args.candidate,
        "speech_corpus": {
            "source": str(args.speech_cases) if args.speech_cases else "default 3-case screening",
            "requested_count": corpus_count,
            "scheduled_count": scheduled_count,
            "completed_count": sum(e["stage"] == "speech" for e in events),
            "quality_verified": False,
        },
        "action": args.action,
        "cold_warmup_text": args.warmup_text if args.action in {"run", "diagnose"} else None,
        "load_order": args.load_order,
        "measurement_kind": "instrumented_diagnostic" if instrumented else "unprofiled_measurement",
        "co_residency_test": args.load_order != "isolated",
        "diagnostic_identity": diagnostic_identity(args)
        if args.action in {"run", "diagnose"} and args.candidate in {"bert-int8", "bert-cuda-int8wo"}
        else None,
        "prior_diagnostic": str(args.diagnostic_result) if prior_diagnostic else None,
        "profile_before": before,
        "profile_after_llm_load": loaded_profile,
        "profile_after": after,
        "same_llm_pids_after_loading": loaded_profile.get("llm_pids") == after.get("llm_pids"),
        "profile_unchanged": unchanged,
        "returncode": process.returncode,
        "guard_reason": reason,
        "completed": reason is None
        and process.returncode == 0
        and unchanged
        and sum(e["stage"] == "speech" for e in events) == scheduled_count
        and any(e["stage"] == "completed" for e in events),
        "memory": watch.summary(),
        "total_ram_kib": total_kib,
        "peak_machine_total_minus_available_kib": total_kib - watch.minimum_available_kib,
        "sampled_peak_child_rss_bytes": peak_rss,
        "sampled_phases": phases,
        "memory_sampling_interval_seconds": 0.5,
        "candidate_loading_and_warmup_seconds": next(
            (e["candidate_loading_and_warmup_seconds"] for e in events if e["stage"] == "ready_before_llm"),
            None,
        ),
        "events": events,
        "voice_quality_verified": False,
        "browser_playback_verified": False,
        "genuine_integer_compute_passed": next(
            (e.get("genuine_integer_compute_passed") for e in events if e["stage"] == "completed"), None
        )
        if instrumented
        else (prior_diagnostic or {}).get("genuine_integer_compute_passed"),
        "integer_compute_evidence": "this instrumented diagnostic"
        if instrumented
        else "separate validated diagnostic; formal forward not instrumented",
        "limitations": [
            "Candidate execution and non-silent PCM do not establish pronunciation/voice quality.",
            "CPU BERT INT8 changes precision; Lite also changes frontend/decoder implementation.",
            "BERT candidates retain original cached FP16 reference features; only new target-text BERT forward is quantized.",
            "All compared components share Jetson DRAM; CUDA allocation alone is not whole-machine RAM.",
            "Instrumented diagnostics are not latency or memory comparisons; isolated runs are not co-residency proof.",
        ],
    }
    (args.output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--action",
        choices=("inspect", "int8-operator", "cuda-int8-operator", "diagnose", "run"),
        default="inspect",
    )
    parser.add_argument(
        "--candidate",
        choices=("compact", "bert-int8", "bert-cuda-int8wo", "lite"),
        default="bert-cuda-int8wo",
    )
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("TUBER_ROOT", ".")))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--voice", default="whale_sovits_v2")
    parser.add_argument("--base-torch-root", type=Path, default=Path("/opt/venv"))
    parser.add_argument("--lite-models", type=Path)
    parser.add_argument("--official-source", type=Path)
    parser.add_argument(
        "--speech-cases", type=Path, help="Ordered JSON texts array; optional voice_id/seed must match"
    )
    parser.add_argument("--diagnostic-result", type=Path)
    parser.add_argument("--load-order", choices=("llm-first", "tts-first", "isolated"), default="llm-first")
    parser.add_argument("--bert-engine", default="qnnpack")
    parser.add_argument(
        "--warmup-text", default=WARMUP_TEXT, help="Complete cold-stage utterance before LLM load"
    )
    parser.add_argument(
        "--cuda-quantization",
        choices=("weight-only", "dynamic-activation"),
        default="weight-only",
        help="Micro-probe only; full bert-cuda-int8wo remains weight-only",
    )
    parser.add_argument("--deadline", type=int, default=180)
    parser.add_argument(
        "--execute", action="store_true", help="Only after an explicit GPU/CPU experiment handoff"
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.root = args.root.resolve()
    for name in ("lite_models", "official_source", "diagnostic_result", "speech_cases"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    if args.action == "inspect":
        print(json.dumps(inspect_environment(args), ensure_ascii=False, indent=2))
        return 0
    if not args.execute:
        parser.error("Execution is separate from inspection; wait for ownership handoff then pass --execute")
    if not args.warmup_text.strip():
        parser.error("Cold warmup cannot be omitted")
    if args.action in {"run", "diagnose"} and args.cuda_quantization != "weight-only":
        parser.error("Dynamic-activation INT8 is only a micro-probe, not an implemented full voice candidate")
    require_original_torch(args)
    if args.action in {"int8-operator", "cuda-int8-operator"} and args.load_order != "isolated":
        parser.error(
            "Operator micro-probes require guarded isolated mode with project LLM/TTS externally stopped"
        )
    if platform.system() != "Linux" or platform.machine() not in {"aarch64", "arm64"}:
        parser.error("Measured candidates require the target Linux aarch64 environment")
    if args.output is None or not args.output.resolve().is_relative_to(args.root / "runtime"):
        parser.error("Measured output must be in this project's runtime directory")
    args.output = args.output.resolve()
    if not args.child and (args.output / "result.json").exists():
        parser.error("Use a fresh output directory; existing candidate evidence is not overwritten")
    args.output.mkdir(parents=True, exist_ok=True)
    if args.child:
        child(args)
        return 0
    result = measured(args)
    print(json.dumps({key: result[key] for key in ("candidate", "completed", "guard_reason", "memory")}))
    if args.action in {"int8-operator", "cuda-int8-operator"} and result.get("completed"):
        return 0 if result.get("genuine_integer_compute_passed") else 2
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
