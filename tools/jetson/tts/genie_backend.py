"""Fixed-voice, CPU-only Genie ONNX runtime behind the buffered speech service.

Model conversion/quantization belongs to the offline bundle preparer. Runtime
loads only its checked files and never expands FP16 weights, imports evaluation
tools, downloads models, or accepts model paths from a speech request.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import time
import types
from pathlib import Path

import compact

BACKEND = "genie_onnx_int8"
MAX_SAMPLES = 10_000_000
SAMPLE_RATE = 32000
_ARENA_REGISTERED = False
FIXED_PARAMETERS = {
    "text_lang": "zh",
    "top_k": 5,
    "top_p": 1.0,
    "temperature": 1.0,
    "repetition_penalty": 1.35,
    "text_split_method": "cut5",
    "batch_size": 1,
    "speed_factor": 1.0,
    "batch_threshold": 0.75,
    "split_bucket": True,
    "parallel_infer": True,
}


def _sibling(name):
    """Import only a fixed adjacent project helper, independent of cwd/sys.path."""
    if name not in {"genie_bundle", "genie_bert", "genie_sampling"}:
        raise ValueError("Unknown fixed speech helper")
    key = "_tuber_tts_" + name
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[key] = module
    return sys.modules[key]


def validate_params(params):
    """Reject unsupported graph controls before synthesis; never ignore knobs."""
    for key, value in FIXED_PARAMETERS.items():
        actual = params.get(key, value)
        if isinstance(value, bool):
            valid = type(actual) is bool and actual is value
        elif isinstance(value, str):
            valid = isinstance(actual, str) and actual == value
        else:
            valid = type(actual) in (int, float) and actual == value
        if not valid:
            raise ValueError(f"Genie backend requires {key}={value}")
    seed = params.get("seed", -1)
    if type(seed) is not int or not -1 <= seed <= 4294967295:
        raise ValueError("Seed must be -1 or an unsigned 32-bit integer")
    interval = params.get("fragment_interval", 0.3)
    if type(interval) not in (int, float) or not math.isfinite(interval):
        raise ValueError("Speech fragment interval must be finite")
    if max(interval, 0.01) * SAMPLE_RATE > MAX_SAMPLES:
        raise ValueError("Speech interval exceeds the buffered audio budget")


def postprocess(segments, *, fragment_interval=0.3):
    """Original v2 per-fragment normalization, trailing gaps and PCM conversion.

    The final fragment also has a gap. No float WAV clipping, loudness processing,
    or resampling is substituted for upstream's multiply-and-cast operation.
    """
    import numpy as np

    interval = max(fragment_interval, 0.01)
    gap_samples = int(SAMPLE_RATE * interval)
    total = sum(int(segment.size) + gap_samples for segment in segments)
    if not segments or total > MAX_SAMPLES:
        raise RuntimeError("Speech exceeded the buffered audio budget or produced no fragments")
    parts = []
    for segment in segments:
        if segment.ndim != 1 or not segment.size or not np.isfinite(segment).all():
            raise RuntimeError("Vocoder returned an invalid audio fragment")
        segment = segment.copy()
        peak = np.abs(segment).max()
        if peak > 1:
            segment /= peak
        parts.append(np.concatenate([segment, np.zeros(gap_samples, dtype=segment.dtype)]))
    return (np.concatenate(parts) * 32768).astype(np.int16)


def _options(ort):
    options = ort.SessionOptions()
    options.intra_op_num_threads, options.inter_op_num_threads = 6, 1
    options.add_session_config_entry("session.use_env_allocators", "1")
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return options


def _session(path, ort, *, held=None):
    """Share immutable VITS external initializers without a runtime FP32 copy."""
    options = _options(ort)
    if held is not None:
        import numpy as np
        import onnx

        graph = onnx.load(path, load_external_data=False).graph
        for tensor in graph.initializer:
            if tensor.data_location != onnx.TensorProto.EXTERNAL:
                continue
            entries = {item.key: item.value for item in tensor.external_data}
            if tensor.data_type != onnx.TensorProto.FLOAT or entries.get("location") != "vits_fp32.bin":
                raise ValueError("VITS requires its checked adjacent FP32 external weights")
            offset, length = int(entries.get("offset", 0)), int(entries["length"])
            if offset % 4 or length % 4 or length != math.prod(tensor.dims) * 4:
                raise ValueError("Invalid VITS external tensor slice")
            if "array" not in held:
                held["array"] = np.memmap(path.parent / "vits_fp32.bin", mode="r", dtype=np.float32)
                held["values"] = []
            if offset + length > held["array"].nbytes:
                raise ValueError("VITS external tensor exceeds its checked file")
            view = held["array"][offset // 4 : (offset + length) // 4].reshape(tuple(tensor.dims))
            value = ort.OrtValue.ortvalue_from_numpy(view)
            held["values"].append(value)
            options.add_initializer(tensor.name, value)
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=options)


def _frontend(root, tokenizer_path, bert_directory, ort):
    import torch
    from transformers import AutoTokenizer

    torch.set_num_threads(6)
    torch.set_num_interop_threads(1)
    helper = _sibling("genie_bert")
    upstream = compact._upstream(root)
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, local_files_only=True, trust_remote_code=False)
    session = helper.load_bert_session(bert_directory, options=_options(ort))
    frontend = upstream.TextPreprocessor(session, tokenizer, torch.device("cpu"))

    def feature(self, text, word2ph):
        hidden = helper.feature(self.bert_model, self.tokenizer(text, return_tensors="np"))[0][1:-1]
        if len(word2ph) != len(text) or hidden.shape != (len(text), 1024):
            raise RuntimeError("Invalid dynamic original-tokenizer ONNX BERT features")
        hidden = torch.from_numpy(hidden)
        return torch.cat([hidden[index].repeat(count, 1) for index, count in enumerate(word2ph)]).T

    frontend.get_bert_feature = types.MethodType(feature, frontend)
    return frontend


class GeniePipeline:
    backend = BACKEND

    def __init__(self, root, voice_id, bundle, *, frontend, encoder, first, stage, vocoder, reference, held):
        self.root, self.voice_id, self.bundle = root, voice_id, bundle
        self.frontend, self.encoder = frontend, encoder
        self.first, self.stage, self.vocoder = first, stage, vocoder
        self.reference, self._held = reference, held
        self.fixed_reference_metadata = bundle["reference_metadata"]
        self.fixed_reference_signature = compact.reference_signature(
            root, voice_id=voice_id, metadata=self.fixed_reference_metadata
        )
        self.backend_identity = bundle["identity"]
        if (
            not isinstance(self.backend_identity, str)
            or len(self.backend_identity) != 64
            or any(letter not in "0123456789abcdef" for letter in self.backend_identity)
        ):
            raise ValueError("Genie bundle needs a stable SHA256 identity")
        self.cache_info = {"bundle_identity": self.backend_identity, "reference_precomputed": True}
        self.last_diagnostics = None

    def verify_assets(self):
        _sibling("genie_bundle").verify_unchanged(self.bundle["stat_signature"])

    def validate_params(self, params):
        validate_params(params)

    def run(self, params):
        import numpy as np

        self.validate_params(params)
        self.verify_assets()
        started = time.monotonic()
        diagnostics = {"backend": BACKEND, "bundle_identity": self.backend_identity, "complete": False}
        self.last_diagnostics = diagnostics
        items = self.frontend.preprocess(params["text"], "zh", "cut5")
        if not items:
            raise RuntimeError("Speech produced no dynamic Chinese text features")
        cache = self.bundle["reference_arrays"]
        prompts = cache["prompt_semantic"].astype(np.int64)[None, :]
        encoder = self.encoder

        class CachedEncoder:
            def run(self, names, feed):
                return [encoder.run(None, {k: v for k, v in feed.items() if k != "ssl_content"})[0], prompts]

        sampling = _sibling("genie_sampling")
        seed = params.get("seed", -1)
        sampler = sampling.SamplingSessions(self.first, self.stage, self.vocoder, seed=seed)
        outputs, segment_diagnostics = [], []
        total_frames = 0
        diagnostics.update(seed=seed, sampling_entropy=int(sampler.entropy), segments=segment_diagnostics)
        for index, item in enumerate(items):
            phones = np.asarray([item["phones"]], dtype=np.int64)
            bert = item["bert_features"].T.contiguous().numpy().astype(np.float32)
            if not phones.size or not np.any(bert) or not np.isfinite(bert).all():
                raise RuntimeError("Invalid dynamic Chinese phoneme/BERT features")
            selected = sampler.segment(index)
            try:
                semantic, segment = sampling.decode_segment(
                    CachedEncoder(),
                    selected.first,
                    selected.stage,
                    ref_seq=cache["reference_phones"].astype(np.int64)[None, :],
                    ref_bert=cache["reference_bert"].T.astype(np.float32),
                    text_seq=phones,
                    text_bert=bert,
                    ssl_content=np.empty((0,), np.float32),
                    segment_index=index,
                )
            except sampling.IncompleteSynthesis as error:
                segment_diagnostics.append(error.diagnostics)
                raise
            wave = selected.vocoder.run(
                None, {"text_seq": phones, "pred_semantic": semantic, "ref_audio": self.reference[None, :]}
            )[0].squeeze()
            if wave.ndim != 1 or not wave.size or not np.isfinite(wave).all():
                raise RuntimeError("Vocoder returned invalid audio")
            gap = int(SAMPLE_RATE * max(params.get("fragment_interval", 0.3), 0.01))
            total_frames += int(wave.size) + gap
            if total_frames > MAX_SAMPLES:
                raise RuntimeError("Speech exceeded the buffered audio budget")
            segment_diagnostics.append({**segment, "raw_frames": int(wave.size)})
            outputs.append(wave)
        interval = max(params.get("fragment_interval", 0.3), 0.01)
        pcm = postprocess(outputs, fragment_interval=interval)
        if not pcm.any():
            raise RuntimeError("Speech returned silent audio")
        diagnostics.update(
            complete=True,
            seconds=time.monotonic() - started,
            segment_count=len(outputs),
            raw_frames=sum(wave.size for wave in outputs),
            output_frames=int(pcm.size),
            fragment_interval=interval,
            postprocess="original_v2_peak_per_fragment_gap_after_each_pcm16",
        )
        yield SAMPLE_RATE, pcm


def create_pipeline(root, *, voice_id):
    import numpy as np
    import onnxruntime as ort
    import soundfile as sf

    global _ARENA_REGISTERED
    root = Path(root).resolve()
    bundle = _sibling("genie_bundle").verify_bundle(root, voice_id)
    if not _ARENA_REGISTERED:
        info = ort.OrtMemoryInfo("Cpu", ort.OrtAllocatorType.ORT_ARENA_ALLOCATOR, 0, ort.OrtMemType.DEFAULT)
        ort.create_and_register_allocator(info, None)
        _ARENA_REGISTERED = True
    paths = bundle["paths"]
    frontend = _frontend(root, paths["tokenizer"], paths["bert_dir"], ort)
    held = {}
    encoder, first, stage = (_session(paths[name], ort) for name in ("encoder", "first", "stage"))
    vocoder = _session(paths["vits"], ort, held=held)
    reference, rate = sf.read(bundle["reference_metadata"]["identity"]["paths"]["reference"], dtype="float32")
    if reference.ndim != 1 or rate != SAMPLE_RATE or not np.isfinite(reference).all():
        raise ValueError("Genie requires its configured mono32k reference without implicit resampling")
    pipeline = GeniePipeline(
        root,
        voice_id,
        bundle,
        frontend=frontend,
        encoder=encoder,
        first=first,
        stage=stage,
        vocoder=vocoder,
        reference=reference,
        held=held,
    )
    pipeline.verify_assets()
    return pipeline
