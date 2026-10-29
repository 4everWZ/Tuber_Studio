"""Evaluation-only fixed-reference v2 pipeline; never selects a production profile.

The cache is plain NPZ plus checked JSON, not pickle. Target text still uses the
original Chinese frontend and the original 22-layer BERT feature representation;
only the unused final two BERT layers and masked-LM head are discarded.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import zipfile
from pathlib import Path

FORMAT = 1
MAX_CACHE_BYTES = 16 * 1024 * 1024
ARRAYS = {"prompt_semantic", "reference_spec", "reference_phones", "reference_bert"}


def _file(root, raw, bases):
    root = Path(root).resolve()
    raw = Path(raw)
    path = raw if raw.is_absolute() else root / raw
    if ".." in raw.parts or not path.is_relative_to(root):
        raise ValueError("Reference feature input is outside this project")
    for component in [path, *path.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("Reference feature links are disabled")
    path = path.resolve(strict=True)
    if not path.is_file() or not any(path.is_relative_to(root / base) for base in bases):
        raise ValueError("Reference feature input is outside its fixed directory")
    return path


def _digest(path):
    with path.open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256").hexdigest()
        # This project-owned file alone may have just populated page cache.
        if hasattr(os, "posix_fadvise"):
            try:
                os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except OSError:
                pass
    return result


def _source():
    return Path(os.environ.get("TUBER_GPT_SOVITS_ROOT", "/opt/GPT-SoVITS-v2")).resolve()


def _splits():
    path = _source() / "GPT_SoVITS/TTS_infer_pack/text_segmentation_method.py"
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "splits" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, set) and all(isinstance(v, str) for v in value):
                return value
    raise ValueError("Unsupported upstream prompt punctuation")


def _normal_prompt(text, language):
    if not isinstance(text, str):
        raise ValueError("Reference transcript must be text")
    text = text.strip("\n")
    if not text:
        raise ValueError("The fixed reference needs its original transcript")
    if text[-1] not in _splits():
        text += "." if language == "en" else "。"
    return text


def _settings(root, voice_id):
    import yaml

    root = Path(root).resolve()
    voices = json.loads((root / "config/voices.json").read_text(encoding="utf-8"))
    voice = next((v for v in voices if v.get("id") == voice_id and v.get("provider") == "gpt_sovits"), None)
    if voice is None or not re.fullmatch(r"[A-Za-z0-9_-]+", voice_id):
        raise ValueError("Unknown fixed GPT-SoVITS voice")
    config_file = _file(root, "runtime/tts/tts_infer.yaml", ("runtime/tts",))
    data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    config = data["custom"]
    if config.get("version") != "v2":
        raise ValueError("This candidate only supports the existing v2 voice")
    reference = _file(root, voice["server_reference_path"], ("runtime/tts/ref", "assets"))
    paths = {
        "reference": reference,
        "t2s": _file(root, config["t2s_weights_path"], ("runtime/tts/weights",)),
        "vits": _file(root, config["vits_weights_path"], ("runtime/tts/weights",)),
    }
    for name, key in (("bert", "bert_base_path"), ("hubert", "cnhuhbert_base_path")):
        base = Path(config[key])
        base = base if base.is_absolute() else root / base
        base = _file(root, base / "config.json", ("runtime/tts/pretrained",)).parent
        for candidate in sorted(base.iterdir()):
            if candidate.is_file():
                paths[name + "/" + candidate.name] = _file(root, candidate, ("runtime/tts/pretrained",))
    bert = json.loads(paths["bert/config.json"].read_text(encoding="utf-8"))
    if (bert.get("model_type"), bert.get("num_hidden_layers"), bert.get("hidden_size")) != ("bert", 24, 1024):
        raise ValueError("The candidate requires the verified 24-layer BERT architecture")
    language = voice.get("params", {}).get("prompt_lang", "zh")
    identity = {
        "upstream": "20240821v2",
        "voice_id": voice_id,
        "voice_version": voice.get("version"),
        "weights_version": voice.get("weights_version"),
        "reference_text": voice["reference_text"],
        "prompt_lang": language,
        "prompt_text": _normal_prompt(voice["reference_text"], language),
        "config_sha256": _digest(config_file),
        "compatibility_sha256": _digest(root / "tools/jetson/tts/serve.py"),
        "paths": {key: str(path) for key, path in paths.items()},
        "sha256": {key: _digest(path) for key, path in paths.items()},
        "source_sha256": {
            name: _digest(_source() / name)
            for name in (
                "GPT_SoVITS/TTS_infer_pack/TTS.py",
                "GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py",
                "GPT_SoVITS/TTS_infer_pack/text_segmentation_method.py",
            )
        },
        "precision": "float16" if config["is_half"] and str(config["device"]) != "cpu" else "float32",
    }
    directory = root / "runtime/tts/reference_features" / voice_id
    for component in [directory, *directory.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("Reference feature directory cannot use links")
    directory.mkdir(parents=True, exist_ok=True)
    return root, directory, config_file, identity


def _array_specs(arrays, precision, *, fft_bins=1025):
    if set(arrays) != ARRAYS:
        raise ValueError("Reference feature arrays do not match the fixed format")
    specs = {name: {"dtype": str(a.dtype), "shape": list(a.shape)} for name, a in arrays.items()}
    semantic, phones, bert, spec = (
        specs[n] for n in ("prompt_semantic", "reference_phones", "reference_bert", "reference_spec")
    )
    if any(
        s["dtype"] != "int64" or len(s["shape"]) != 1 or not 1 <= s["shape"][0] <= 2048
        for s in (semantic, phones)
    ):
        raise ValueError("Invalid reference semantic or phoneme array")
    if bert != {"dtype": precision, "shape": [1024, phones["shape"][0]]}:
        raise ValueError("Invalid reference BERT feature shape/precision")
    if (
        spec["dtype"] != precision
        or len(spec["shape"]) != 3
        or spec["shape"][:2] != [1, fft_bins]
        or not 1 <= spec["shape"][2] <= 4096
    ):
        raise ValueError("Invalid reference spectrogram shape/precision")
    return specs


def _validate_values(np, arrays):
    if any(not np.isfinite(a).all() for a in arrays.values()):
        raise ValueError("Reference features must be finite")
    if any((arrays[name] < 0).any() for name in ("prompt_semantic", "reference_phones")):
        raise ValueError("Reference token IDs must be nonnegative")


def save_reference(root, pipeline, *, voice_id="whale_sovits_v2"):
    """Save an already computed original prompt cache without loading models."""
    _, directory, _, identity = _settings(root, voice_id)
    return _save_reference(pipeline, directory, identity)


def _save_reference(pipeline, directory, identity):
    import numpy as np

    cache = pipeline.prompt_cache
    if (
        (cache.get("ref_audio_path"), cache.get("prompt_text"), cache.get("prompt_lang"))
        != (identity["paths"]["reference"], identity["prompt_text"], identity["prompt_lang"])
        or cache.get("aux_ref_audio_paths")
        or len(cache.get("refer_spec", [])) != 1
    ):
        raise ValueError("The pipeline does not contain the exact computed fixed reference")
    for key, field in (("t2s", "t2s_weights_path"), ("vits", "vits_weights_path")):
        if str(Path(getattr(pipeline.configs, field)).resolve()) != identity["paths"][key]:
            raise ValueError("Reference pipeline weights do not match this project profile")
    for key, field in (("bert", "bert_base_path"), ("hubert", "cnhuhbert_base_path")):
        if (
            Path(getattr(pipeline.configs, field)).resolve()
            != Path(identity["paths"][key + "/config.json"]).parent
        ):
            raise ValueError("Reference pipeline encoder identity does not match this profile")
    arrays = {
        "prompt_semantic": cache["prompt_semantic"].detach().cpu().numpy().copy(),
        "reference_spec": cache["refer_spec"][0].detach().cpu().numpy().copy(),
        "reference_phones": np.asarray(cache["phones"], dtype=np.int64),
        "reference_bert": cache["bert_features"].detach().cpu().numpy().copy(),
    }
    specs = _array_specs(arrays, identity["precision"], fft_bins=pipeline.configs.filter_length // 2 + 1)
    _validate_values(np, arrays)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".npz", delete=False) as target:
        temporary = Path(target.name)
        np.savez(target, **arrays)
        target.flush()
        os.fsync(target.fileno())
    try:
        metadata = {
            "format": FORMAT,
            "identity": identity,
            "arrays": specs,
            "norm_text": cache["norm_text"],
            "npz_sha256": _digest(temporary),
        }
        metadata_file = temporary.with_suffix(".json")
        metadata_file.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, directory / "features.npz")
        os.replace(metadata_file, directory / "metadata.json")
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_suffix(".json").unlink(missing_ok=True)
    return metadata


def _read_bundle(directory, identity):
    import numpy as np

    metadata_file, archive = directory / "metadata.json", directory / "features.npz"
    if metadata_file.is_symlink() or archive.is_symlink() or metadata_file.stat().st_size > 128 * 1024:
        raise ValueError("Unsafe reference feature cache")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    if metadata.get("format") != FORMAT or metadata.get("identity") != identity:
        raise ValueError("Fixed reference identity changed; regenerate its features")
    if archive.stat().st_size > MAX_CACHE_BYTES or _digest(archive) != metadata.get("npz_sha256"):
        raise ValueError("Reference feature archive identity mismatch")
    with zipfile.ZipFile(archive) as files:
        if {f.filename for f in files.infolist()} != {name + ".npy" for name in ARRAYS} or sum(
            f.file_size for f in files.infolist()
        ) > MAX_CACHE_BYTES:
            raise ValueError("Reference feature archive exceeds the fixed format")
    with np.load(archive, allow_pickle=False) as loaded:
        arrays = {name: loaded[name].copy() for name in loaded.files}
    if _array_specs(arrays, identity["precision"]) != metadata.get("arrays"):
        raise ValueError("Reference feature tensor metadata mismatch")
    _validate_values(np, arrays)
    if not isinstance(metadata.get("norm_text"), str):
        raise ValueError("Missing original normalized reference transcript")
    return metadata, arrays


def _upstream(root):
    source = _source()
    os.chdir(source)
    sys.path[:0] = [str(source), str(source / "GPT_SoVITS")]
    if not any(type(f).__name__ == "CompatibilityFinder" for f in sys.meta_path):
        spec = importlib.util.spec_from_file_location(
            "_compact_v2_compat", root / "tools/jetson/tts/serve.py"
        )
        compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compat)
        compat.check_source(source)
        sys.meta_path.insert(0, compat.CompatibilityFinder())
    return importlib.import_module("GPT_SoVITS.TTS_infer_pack.TTS")


def _private_config(directory, config_file):
    path = directory / "evaluation-config.yaml"
    if path.is_symlink():
        raise ValueError("Evaluation config cannot use links")
    path.write_bytes(config_file.read_bytes())
    return str(path)


def _compute_reference(pipeline, identity):
    pipeline.set_ref_audio(identity["paths"]["reference"])
    text, language = identity["prompt_text"], identity["prompt_lang"]
    # Match the original run call exactly, including its default phoneme version.
    phones, bert, normalized = pipeline.text_preprocessor.segment_and_extract_feature_for_text(text, language)
    pipeline.prompt_cache.update(
        prompt_text=text, prompt_lang=language, phones=phones, bert_features=bert, norm_text=normalized
    )


def prepare(root, *, voice_id="whale_sovits_v2"):
    root, directory, config, identity = _settings(root, voice_id)
    upstream = _upstream(root)
    pipeline = upstream.TTS(_private_config(directory, config))
    try:
        _compute_reference(pipeline, identity)
        return _save_reference(pipeline, directory, identity)
    finally:
        del pipeline
        import gc

        import torch

        gc.collect()
        if torch.cuda.is_initialized():
            torch.cuda.empty_cache()


def compute_reference(root, pipeline, *, voice_id="whale_sovits_v2"):
    """Precompute only the original reference features in an existing baseline."""
    _, directory, _, identity = _settings(root, voice_id)
    _compute_reference(pipeline, identity)
    return _save_reference(pipeline, directory, identity)


def create_pipeline(root, *, voice_id="whale_sovits_v2"):
    root, directory, config, identity = _settings(root, voice_id)
    metadata, arrays = _read_bundle(directory, identity)
    upstream = _upstream(root)
    import torch

    class CompactTTS(upstream.TTS):
        def init_cnhuhbert_weights(self, base_path):
            self.cnhuhbert_model = None

        def init_bert_weights(self, base_path):
            from transformers import AutoModelForMaskedLM, AutoTokenizer

            precision = torch.float16 if identity["precision"] == "float16" else torch.float32
            masked = AutoModelForMaskedLM.from_pretrained(base_path, torch_dtype=precision)
            if masked.config.model_type != "bert" or len(masked.bert.encoder.layer) != 24:
                raise ValueError("Unsupported fixed BERT backbone")
            backbone = masked.bert
            backbone.encoder.layer = torch.nn.ModuleList(list(backbone.encoder.layer)[:22])
            backbone.config.num_hidden_layers = 22
            self.bert_tokenizer = AutoTokenizer.from_pretrained(base_path)
            self.bert_model = backbone.eval().to(self.configs.device)
            del masked

        def set_ref_audio(self, path):
            if str(_file(root, path, ("runtime/tts/ref", "assets"))) != identity["paths"]["reference"]:
                raise ValueError("This evaluation pipeline has one fixed reference")

        def run(self, inputs):
            self.set_ref_audio(inputs.get("ref_audio_path", ""))
            if (
                inputs.get("aux_ref_audio_paths")
                or inputs.get("prompt_lang") != identity["prompt_lang"]
                or _normal_prompt(inputs.get("prompt_text", ""), inputs.get("prompt_lang"))
                != identity["prompt_text"]
            ):
                raise ValueError("Reference transcript/language does not match the feature artifact")
            yield from super().run(inputs)

    pipeline = CompactTTS(_private_config(directory, config))
    pipeline.prompt_cache.update(
        ref_audio_path=identity["paths"]["reference"],
        prompt_semantic=torch.from_numpy(arrays["prompt_semantic"]).to(pipeline.configs.device),
        refer_spec=[torch.from_numpy(arrays["reference_spec"]).to(pipeline.configs.device)],
        prompt_text=identity["prompt_text"],
        prompt_lang=identity["prompt_lang"],
        phones=arrays["reference_phones"].tolist(),
        bert_features=torch.from_numpy(arrays["reference_bert"]).to(pipeline.configs.device),
        norm_text=metadata["norm_text"],
        aux_ref_audio_paths=[],
    )

    def feature(preprocessor, text, word2ph):
        with torch.no_grad():
            inputs = preprocessor.tokenizer(text, return_tensors="pt")
            inputs = {key: value.to(preprocessor.device) for key, value in inputs.items()}
            result = preprocessor.bert_model(**inputs, output_hidden_states=False, return_dict=True)
            result = result.last_hidden_state[0].cpu()[1:-1]
        assert len(word2ph) == len(text)
        return torch.cat([result[i].repeat(word2ph[i], 1) for i in range(len(word2ph))], dim=0).T

    pipeline.text_preprocessor.get_bert_feature = types.MethodType(feature, pipeline.text_preprocessor)
    return pipeline


def ar_storage_report(pipeline):
    """Compare real storage identity, without assuming named weights are copies."""
    decoder = pipeline.t2s_model.model
    registered = {}
    for tensor in decoder.parameters():
        storage = tensor.untyped_storage()
        registered[(str(tensor.device), storage.data_ptr())] = storage.nbytes()
    script = {}
    for block in decoder.t2s_transformer.blocks:
        for tensor in [
            block.qkv_w,
            block.qkv_b,
            block.out_w,
            block.out_b,
            block.norm_w1,
            block.norm_b1,
            block.norm_w2,
            block.norm_b2,
            block.mlp.w1,
            block.mlp.b1,
            block.mlp.w2,
            block.mlp.b2,
        ]:
            storage = tensor.untyped_storage()
            script[(str(tensor.device), storage.data_ptr())] = storage.nbytes()
    return {
        "registered_bytes": sum(registered.values()),
        "script_bytes": sum(script.values()),
        "shared_bytes": sum(size for key, size in script.items() if key in registered),
        "script_only_bytes": sum(size for key, size in script.items() if key not in registered),
    }
