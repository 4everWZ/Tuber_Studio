"""Guarded v2 training-wrapper removal; no inference math is replaced here."""

import ast

WRAPPER_SOURCE = '''"""Inference-only checkpoint wrapper; model.* keys remain unchanged."""
from torch import nn
from AR.models.t2s_model import Text2SemanticDecoder

class Text2SemanticInferenceModule(nn.Module):
    def __init__(self, config, output_dir=None, is_train=False):
        super().__init__()
        if is_train:
            raise ValueError("This wrapper only supports inference")
        self.config = config
        self.top_k = 3
        self.model = Text2SemanticDecoder(config=config, top_k=self.top_k)

    def load_state_dict(self, state_dict, strict=True, assign=False):
        if strict is not True:
            raise ValueError("Inference checkpoints require strict loading")
        return super().load_state_dict(state_dict, strict=True, assign=assign)

Text2SemanticLightningModule = Text2SemanticInferenceModule
'''

DECODER_MARKER = "# Tuber v2 inference-only: unused training metric removed.\n"
UTILITIES_MARKER = "# Tuber v2 inference-only: original audio utilities without UI imports.\n"
REFERENCE_MARKER = "# Tuber v2 inference-only: reference-only dependencies are lazy.\n"


def _class(tree, name):
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    if len(classes) != 1:
        raise RuntimeError("Unsupported GPT-SoVITS v2 class layout")
    return classes[0]


def _same(node, expected):
    return ast.dump(node, include_attributes=False) == ast.dump(
        ast.parse(expected).body[0], include_attributes=False
    )


def transform_lightning(source):
    if source == WRAPPER_SOURCE:
        return source
    module = _class(ast.parse(source), "Text2SemanticLightningModule")
    if (
        len(module.bases) != 1
        or not isinstance(module.bases[0], ast.Name)
        or module.bases[0].id != "LightningModule"
    ):
        raise RuntimeError("Unsupported original Lightning wrapper base")
    methods = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
    if set(methods) != {"__init__", "training_step", "validation_step", "configure_optimizers"}:
        raise RuntimeError("Unexpected original Lightning wrapper methods")
    body = methods["__init__"].body
    expected = [
        "super().__init__()",
        "self.config = config",
        "self.top_k = 3",
        "self.model = Text2SemanticDecoder(config=config, top_k=self.top_k)",
    ]
    if len(body) != 7 or any(not _same(node, text) for node, text in zip(body[:4], expected)):
        raise RuntimeError("Original inference constructor changed")
    if not _same(body[4], "pretrained_s1 = config.get('pretrained_s1')"):
        raise RuntimeError("Original pretrained branch changed")
    # The only remaining initialization branches must be guarded by is_train.
    for node, expression in zip(body[5:], ("pretrained_s1 and is_train", "is_train")):
        if (
            not isinstance(node, ast.If)
            or ast.dump(node.test) != ast.dump(ast.parse(expression, mode="eval").body)
            or node.orelse
        ):
            raise RuntimeError("Unexpected inference-time Lightning initialization")
    return WRAPPER_SOURCE


def transform_decoder(source):
    tree = ast.parse(source)
    decoder = _class(tree, "Text2SemanticDecoder")
    constructors, imports = [], []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "torchmetrics.classification":
            if len(node.names) != 1 or node.names[0].name != "MulticlassAccuracy" or node.names[0].asname:
                raise RuntimeError("Unexpected torchmetrics import")
            imports.append(node)
    for method in decoder.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        references = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute) and node.attr == "ar_accuracy_metric"
        ]
        if references and method.name not in {"__init__", "forward", "forward_old"}:
            raise RuntimeError("Training accuracy metric became an inference dependency")
        for node in method.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "ar_accuracy_metric" for t in node.targets
            ):
                if method.name != "__init__":
                    raise RuntimeError("Unexpected accuracy metric assignment")
                expected = "self.ar_accuracy_metric = MulticlassAccuracy(self.vocab_size, top_k=top_k, average='micro', multidim_average='global', ignore_index=self.EOS)"
                if not _same(node, expected):
                    raise RuntimeError("Original training metric construction changed")
                constructors.append(node)
    if source.startswith(DECODER_MARKER):
        if imports or constructors:
            raise RuntimeError("Inconsistent inference-only decoder marker")
        return source
    if len(imports) != 1 or len(constructors) != 1:
        raise RuntimeError("Expected the pinned v2 training metric exactly once")
    if sum(isinstance(node, ast.Name) and node.id == "MulticlassAccuracy" for node in ast.walk(tree)) != 1:
        raise RuntimeError("Unexpected additional use of the training metric class")
    # Keep every other source line, including all infer/attention/sampling code.
    lines = source.splitlines(keepends=True)
    removed = {i for node in imports + constructors for i in range(node.lineno - 1, node.end_lineno)}
    return DECODER_MARKER + "".join(line for i, line in enumerate(lines) if i not in removed)


def transform_audio_utilities(source):
    """Keep original decode/path functions; discard unrelated UI-only helpers."""
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if not {"load_audio", "clean_path"}.issubset(functions):
        raise RuntimeError("Expected original v2 audio/path utilities")
    originals = []
    allowed_globals = {
        "os",
        "traceback",
        "ffmpeg",
        "np",
        "i18n",
        "clean_path",
        "Exception",
        "RuntimeError",
        "str",
    }
    for name, arguments in (("load_audio", ["file", "sr"]), ("clean_path", ["path_str"])):
        function = functions[name]
        if [arg.arg for arg in function.args.args] != arguments or function.decorator_list:
            raise RuntimeError("Original audio utility signature changed")
        local = set(arguments) | {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
        }
        local |= {
            node.name for node in ast.walk(function) if isinstance(node, ast.ExceptHandler) and node.name
        }
        dependencies = {
            node.id
            for node in ast.walk(function)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        } - local
        if dependencies - allowed_globals:
            raise RuntimeError("Original audio utility acquired an unsupported dependency")
        originals.append(ast.get_source_segment(source, function))
    initializers = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "i18n" for t in node.targets)
    ]
    if len(initializers) != 1 or not _same(
        initializers[0], "i18n = I18nAuto(language=os.environ.get('language', 'Auto'))"
    ):
        raise RuntimeError("Original audio translation initialization changed")
    output = (
        UTILITIES_MARKER
        + "import os, traceback\nimport ffmpeg\nimport numpy as np\nfrom tools.i18n.i18n import I18nAuto\n"
    )
    output += ast.get_source_segment(source, initializers[0]) + "\n\n" + "\n\n".join(originals) + "\n"
    return output


def transform_reference_imports(source):
    """Move only reference extraction imports; do not rewrite its operations."""
    imports = {
        "CNHubert": ("from feature_extractor.cnhubert import CNHubert", {"init_cnhuhbert_weights"}),
        "librosa": ("import librosa", {"_set_prompt_semantic"}),
        "load_audio": ("from tools.my_utils import load_audio", {"_get_ref_spec"}),
        "spectrogram_torch": ("from module.mel_processing import spectrogram_torch", {"_get_ref_spec"}),
    }
    tree = ast.parse(source)
    pipeline = _class(tree, "TTS")
    functions = {n.name: n for n in pipeline.body if isinstance(n, ast.FunctionDef)}
    for binding, (_, allowed) in imports.items():
        if not allowed.issubset(functions):
            raise RuntimeError("Original reference extraction methods changed")
        for function in functions.values():
            names = [n for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == binding]
            if function.name == "__init__" and binding == "CNHubert":
                annotations = {id(n.annotation) for n in ast.walk(function) if isinstance(n, ast.AnnAssign)}
                names = [n for n in names if id(n) not in annotations]
            if names and function.name not in allowed:
                raise RuntimeError("A reference-only dependency became part of dynamic synthesis")
    if source.startswith(REFERENCE_MARKER):
        if any(_same(node, text) for node in tree.body for text, _ in imports.values()):
            raise RuntimeError("Inconsistent lazy reference import marker")
        return source
    removed, insertions = set(), {}
    for _, (text, methods) in imports.items():
        nodes = [node for node in tree.body if _same(node, text)]
        if len(nodes) != 1:
            raise RuntimeError("Expected pinned reference import exactly once")
        removed.update(range(nodes[0].lineno - 1, nodes[0].end_lineno))
        for name in methods:
            function = functions[name]
            first = function.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                first = function.body[1]
            insertions.setdefault(first.lineno - 1, []).append(" " * first.col_offset + text + "\n")
    lines = source.splitlines(keepends=True)
    return REFERENCE_MARKER + "".join(
        "".join(insertions.get(i, [])) + (line if i not in removed else "") for i, line in enumerate(lines)
    )
