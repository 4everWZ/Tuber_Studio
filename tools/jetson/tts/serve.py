"""Run GPT-SoVITS 20240821v2 with the image's shared /opt/studio-venv.

No separate Python environment is created, and upstream source is not rewritten.

The old release imports typing aliases implicitly from torch.nn.functional;
torch 2.7 no longer exports all of them. Its original loading order also holds
GPU checkpoints and FP32 models before converting to FP16. The import loader
below makes those imports explicit and loads checkpoint tensors on CPU, then
converts models to their configured FP16 precision before transferring to CUDA.
The final model weights, architecture and requested inference precision stay
the same. No training or decoding operations are replaced.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import runpy
import sys
from functools import wraps
from pathlib import Path


def replace_expected(source: str, old: str, new: str) -> str:
    """Accept pristine v2 or an installation already carrying this patch."""
    if new in source:
        return source
    if old not in source:
        raise RuntimeError("Unsupported GPT-SoVITS source; expected tag 20240821v2")
    return source.replace(old, new)


def transform_tts(source: str) -> str:
    for checkpoint in ("dict_s1", "dict_s2"):
        source = replace_expected(
            source,
            f"{checkpoint} = torch.load(weights_path, map_location=self.configs.device)",
            f'{checkpoint} = torch.load(weights_path, map_location="cpu")',
        )
    for name in ("self.cnhuhbert_model", "self.bert_model"):
        source = replace_expected(
            source,
            f"{name} = {name}.to(self.configs.device)\n"
            '        if self.configs.is_half and str(self.configs.device)!="cpu":\n'
            f"            {name} = {name}.half()",
            'if self.configs.is_half and str(self.configs.device)!="cpu":\n'
            f"            {name} = {name}.half()\n"
            f"        {name} = {name}.to(self.configs.device)",
        )
    for name in ("vits_model", "t2s_model"):
        old = f"{name} = {name}.to(self.configs.device)\n        {name} = {name}.eval()"
        source = replace_expected(
            source,
            old,
            'if self.configs.is_half and str(self.configs.device)!="cpu":\n'
            f"            {name} = {name}.half()\n        " + old,
        )
    return source


def transform_mha(source: str) -> str:
    header = "from typing import Callable, List, Optional, Tuple, Union\nfrom torch import Tensor\n"
    return source if source.startswith(header) else header + source


TRANSFORMS = {
    "AR.modules.patched_mha_with_cache": transform_mha,
    "GPT_SoVITS.TTS_infer_pack.TTS": transform_tts,
    "TTS_infer_pack.TTS": transform_tts,
}


def release_after_run(original):
    """Release allocator caches after the upstream generator's frame is gone.

    Upstream's finally executes with its local tensors still referenced. Repeating
    its existing cache cleanup after closing that frame preserves every yielded
    audio chunk while returning only unused allocations to the shared device.
    """

    @wraps(original)
    def run(self, *args, **kwargs):
        iterator = original(self, *args, **kwargs)
        try:
            for sample_rate, audio in iterator:
                audible = bool(audio.any()) if hasattr(audio, "any") else any(audio)
                if not audible:
                    raise RuntimeError(
                        "TTS returned an empty or silent fallback instead of synthesized speech"
                    )
                yield sample_rate, audio
        finally:
            try:
                iterator.close()
            finally:
                del iterator
                self.empty_cache()

    return run


class CompatibilityLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        super().exec_module(module)
        if module.__name__ in {"GPT_SoVITS.TTS_infer_pack.TTS", "TTS_infer_pack.TTS"}:
            module.TTS.run = release_after_run(module.TTS.run)

    def get_code(self, fullname: str):
        # Compile in memory: never write transformed third-party source or pyc.
        source = self.get_data(self.path).decode("utf-8-sig").replace("\r\n", "\n")
        return compile(TRANSFORMS[fullname](source), self.path, "exec")


class CompatibilityFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in TRANSFORMS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin is None:
            return None
        spec.loader = CompatibilityLoader(fullname, spec.origin)
        return spec


def check_source(source_root: Path) -> None:
    paths = {
        "TTS_infer_pack.TTS": source_root / "GPT_SoVITS/TTS_infer_pack/TTS.py",
        "AR.modules.patched_mha_with_cache": source_root / "GPT_SoVITS/AR/modules/patched_mha_with_cache.py",
    }
    for name, path in paths.items():
        source = path.read_text(encoding="utf-8-sig")
        transformed = TRANSFORMS[name](source)
        assert TRANSFORMS[name](transformed) == transformed, "Compatibility transforms must be idempotent"
        compile(transformed, str(path), "exec")
    print("GPT-SoVITS v2 compatibility source checks passed")


def main() -> None:
    root = Path(os.environ.get("TUBER_ROOT", "/workspace/tuber"))
    source_root = Path(os.environ.get("TUBER_GPT_SOVITS_ROOT", str(root / "runtime/tts/GPT-SoVITS-v2")))
    if sys.argv[1:] == ["--check-source"]:
        check_source(source_root)
        return
    check_source(source_root)
    os.chdir(source_root)
    sys.path[:0] = [str(source_root), str(source_root / "GPT_SoVITS")]
    sys.meta_path.insert(0, CompatibilityFinder())
    api = source_root / "api_v2.py"
    sys.argv[0] = str(api)
    runpy.run_path(str(api), run_name="__main__")


if __name__ == "__main__":
    main()
