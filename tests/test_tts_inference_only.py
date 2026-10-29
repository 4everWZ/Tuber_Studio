import ast
import builtins
import importlib.util
import sys
import types
from pathlib import Path

import pytest

FOLDER = Path(__file__).resolve().parents[1] / "tools/jetson/tts"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, FOLDER / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIGHTNING = """from pytorch_lightning import LightningModule
from AR.models.t2s_model import Text2SemanticDecoder
class Text2SemanticLightningModule(LightningModule):
    def __init__(self, config, output_dir, is_train=True):
        super().__init__()
        self.config = config
        self.top_k = 3
        self.model = Text2SemanticDecoder(config=config, top_k=self.top_k)
        pretrained_s1 = config.get("pretrained_s1")
        if pretrained_s1 and is_train:
            self.load_state_dict({})
        if is_train:
            self.save_hyperparameters()
    def training_step(self): pass
    def validation_step(self): pass
    def configure_optimizers(self): pass
"""

DECODER = """from torchmetrics.classification import MulticlassAccuracy
class Text2SemanticDecoder:
    def __init__(self, config, top_k=3):
        self.vocab_size = 1025
        self.EOS = 1024
        self.ar_accuracy_metric = MulticlassAccuracy(
            self.vocab_size, top_k=top_k, average="micro",
            multidim_average="global", ignore_index=self.EOS,
        )
        self.original_weights = config
    def forward(self, logits, targets):
        return self.ar_accuracy_metric(logits, targets)
    def forward_old(self, logits, targets):
        return self.ar_accuracy_metric(logits, targets)
    def infer_panel(self, x):
        # This stands for the upstream attention/sampling code, kept verbatim.
        return x * 3 + 1
"""


def test_only_metric_import_and_constructor_are_removed_from_decoder():
    adapter = load("inference_only_test", "inference_only.py")
    transformed = adapter.transform_decoder(DECODER)
    assert "import MulticlassAccuracy" not in transformed
    assert "self.ar_accuracy_metric =" not in transformed
    for method in ("infer_panel", "forward", "forward_old"):
        original = next(
            n for n in ast.walk(ast.parse(DECODER)) if isinstance(n, ast.FunctionDef) and n.name == method
        )
        actual = next(
            n for n in ast.walk(ast.parse(transformed)) if isinstance(n, ast.FunctionDef) and n.name == method
        )
        assert ast.get_source_segment(DECODER, original) == ast.get_source_segment(transformed, actual)
    assert adapter.transform_decoder(transformed) == transformed
    compile(transformed, "decoder", "exec")


@pytest.mark.parametrize(
    "source",
    [
        DECODER.replace("return x * 3 + 1", "return self.ar_accuracy_metric(x, x)"),
        DECODER.replace("top_k=top_k", "top_k=7"),
        DECODER.replace("from torchmetrics.classification import MulticlassAccuracy", ""),
        DECODER + "extra_metric = MulticlassAccuracy\n",
    ],
)
def test_decoder_guard_rejects_new_dependencies_or_unknown_source(source):
    adapter = load("inference_only_test", "inference_only.py")
    with pytest.raises(RuntimeError):
        adapter.transform_decoder(source)


@pytest.mark.parametrize(
    "source",
    [
        LIGHTNING.replace("self.top_k = 3", "self.top_k = 4"),
        LIGHTNING.replace(
            "self.save_hyperparameters()", "self.save_hyperparameters()\n        self.new_inference_state = 1"
        ),
        LIGHTNING.replace("if is_train:", "if not is_train:"),
    ],
)
def test_wrapper_guard_rejects_changed_inference_initialization(source):
    adapter = load("inference_only_test", "inference_only.py")
    with pytest.raises(RuntimeError):
        adapter.transform_lightning(source)


def test_inference_wrapper_preserves_config_decoder_topk_and_strict_state_prefix(monkeypatch):
    adapter = load("inference_only_test", "inference_only.py")
    transformed = adapter.transform_lightning(LIGHTNING)
    assert "pytorch_lightning" not in transformed and "torchmetrics" not in transformed
    assert adapter.transform_lightning(transformed) == transformed
    calls = []

    class Module:
        def load_state_dict(self, state, *, strict, assign):
            calls.append((state, strict, assign))
            if set(state) != {"model.weight"}:
                raise RuntimeError("Unexpected checkpoint key")
            return "strict loaded"

    class Decoder:
        def __init__(self, config, top_k):
            self.config, self.top_k = config, top_k

    torch = types.ModuleType("torch")
    torch.nn = types.SimpleNamespace(Module=Module)
    for name in ("AR", "AR.models", "AR.models.t2s_model"):
        module = types.ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["AR.models.t2s_model"].Text2SemanticDecoder = Decoder
    monkeypatch.setitem(sys.modules, "torch", torch)
    namespace = {}
    exec(compile(transformed, "inference-wrapper", "exec"), namespace)
    config = {"model": "unchanged"}
    wrapper = namespace["Text2SemanticLightningModule"](config, is_train=False)
    assert wrapper.config is config and wrapper.model.config is config and wrapper.model.top_k == 3
    state = {"model.weight": object()}
    assert wrapper.load_state_dict(state) == "strict loaded"
    assert calls[-1] == (state, True, False)
    with pytest.raises(RuntimeError, match="checkpoint"):
        wrapper.load_state_dict({**state, "model.ar_accuracy_metric.tp": object()})
    with pytest.raises(ValueError, match="strict"):
        wrapper.load_state_dict(state, strict=False)
    with pytest.raises(ValueError, match="inference"):
        namespace["Text2SemanticLightningModule"](config, is_train=True)


def test_finder_keeps_baseline_default_and_enables_only_explicit_compact_transforms():
    bridge = load("serve_inference_test", "serve.py")
    baseline = bridge.CompatibilityFinder()
    compact = bridge.CompatibilityFinder(inference_only=True)
    assert not baseline.inference_only and "AR.models.t2s_model" not in baseline.transforms
    assert compact.inference_only and compact.transforms["AR.models.t2s_model"]
    assert "tools.my_utils" not in baseline.transforms and "tools.my_utils" in compact.transforms


def test_compact_refuses_modules_preloaded_by_baseline_before_any_path_or_gpu_work(monkeypatch, tmp_path):
    compact = load("compact_inference_test", "compact.py")
    monkeypatch.setitem(sys.modules, "AR.models.t2s_model", types.ModuleType("baseline_decoder"))
    with pytest.raises(RuntimeError, match="separate processes"):
        compact._upstream(tmp_path)


def test_baseline_refuses_compact_module_contamination(monkeypatch):
    bridge = load("serve_inference_test", "serve.py")
    module = types.ModuleType("compact_decoder")
    module._tuber_inference_only = True
    monkeypatch.setitem(sys.modules, "AR.models.t2s_model", module)
    with pytest.raises(RuntimeError, match="separate processes"):
        bridge.CompatibilityFinder()


AUDIO_UTILITIES = """import platform, os, traceback
import ffmpeg
import numpy as np
import gradio as gr
from tools.i18n.i18n import I18nAuto
import pandas as pd
i18n = I18nAuto(language=os.environ.get('language', 'Auto'))
def load_audio(file, sr):
    # The transform must preserve this original function source verbatim.
    file = clean_path(file)
    return np.frombuffer(ffmpeg.decode(file, sr), np.float32).flatten()
def clean_path(path_str: str):
    return path_str.strip(' ').replace('/', os.sep)
def check_details():
    return gr.Warning(pd.DataFrame())
"""


def test_audio_utilities_keep_original_functions_and_drop_only_unneeded_ui_surface():
    adapter = load("inference_only_test", "inference_only.py")
    transformed = adapter.transform_audio_utilities(AUDIO_UTILITIES)
    assert "import gradio" not in transformed and "import pandas" not in transformed
    assert "def check_details" not in transformed
    original = {
        node.name: ast.get_source_segment(AUDIO_UTILITIES, node)
        for node in ast.parse(AUDIO_UTILITIES).body
        if isinstance(node, ast.FunctionDef)
    }
    actual = {
        node.name: ast.get_source_segment(transformed, node)
        for node in ast.parse(transformed).body
        if isinstance(node, ast.FunctionDef)
    }
    assert actual == {name: original[name] for name in ("load_audio", "clean_path")}
    assert adapter.transform_audio_utilities(transformed) == transformed


def test_audio_guard_refuses_a_new_required_dependency_or_changed_i18n_init():
    adapter = load("inference_only_test", "inference_only.py")
    with pytest.raises(RuntimeError, match="dependency"):
        adapter.transform_audio_utilities(
            AUDIO_UTILITIES.replace("file = clean_path(file)", "file = pd.read_csv(file)")
        )
    with pytest.raises(RuntimeError, match="initialization"):
        adapter.transform_audio_utilities(
            AUDIO_UTILITIES.replace("language=os.environ.get('language', 'Auto')", "language='en'")
        )


REFERENCE_SOURCE = """from feature_extractor.cnhubert import CNHubert
import librosa
from tools.my_utils import load_audio
from module.mel_processing import spectrogram_torch
class TTS:
    def __init__(self):
        self.cnhuhbert_model: CNHubert = None
    def init_cnhuhbert_weights(self, base_path):
        self.cnhuhbert_model = CNHubert(base_path)
    def _get_ref_spec(self, path):
        audio = load_audio(path, 32000)
        return spectrogram_torch(audio, 2048)
    def _set_prompt_semantic(self, path):
        return librosa.load(path, sr=16000)
    def run(self, text):
        return text
"""


def test_cached_tts_path_does_not_import_reference_dependencies_and_methods_are_preserved():
    adapter = load("inference_only_test", "inference_only.py")
    transformed = adapter.transform_reference_imports(REFERENCE_SOURCE)
    original = {n.name: n for n in ast.walk(ast.parse(REFERENCE_SOURCE)) if isinstance(n, ast.FunctionDef)}
    actual = {n.name: n for n in ast.walk(ast.parse(transformed)) if isinstance(n, ast.FunctionDef)}
    for name, function in original.items():
        body = [n for n in actual[name].body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        assert [ast.dump(n) for n in body] == [ast.dump(n) for n in function.body]
    assert adapter.transform_reference_imports(transformed) == transformed
    imported = []

    def reject_reference_import(name, *args, **kwargs):
        imported.append(name)
        raise AssertionError("Cached path should not import " + name)

    namespace = {"__builtins__": {**vars(builtins), "__import__": reject_reference_import}}
    exec(compile(transformed, "reference-lazy", "exec"), namespace)
    pipeline = namespace["TTS"]()
    assert pipeline.run("unchanged text") == "unchanged text" and imported == []
    with pytest.raises(AssertionError, match="librosa"):
        pipeline._set_prompt_semantic("reference.wav")


def test_reference_guard_refuses_new_dynamic_use_and_missing_import():
    adapter = load("inference_only_test", "inference_only.py")
    with pytest.raises(RuntimeError, match="dynamic synthesis"):
        adapter.transform_reference_imports(
            REFERENCE_SOURCE.replace("return text", "return librosa.load(text)")
        )
    with pytest.raises(RuntimeError, match="exactly once"):
        adapter.transform_reference_imports(REFERENCE_SOURCE.replace("import librosa\n", ""))
