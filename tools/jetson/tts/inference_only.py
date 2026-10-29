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
