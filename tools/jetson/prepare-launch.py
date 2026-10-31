"""Check the environment and prepare small public presets before replacement."""

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path

from environment import verify


def prepare(root, runtime):
    root, runtime = Path(root).resolve(), Path(runtime).absolute()
    verify(root)
    if runtime.resolve() != runtime or not runtime.is_dir():
        raise ValueError("Runtime must be a real existing data directory")
    spec = importlib.util.spec_from_file_location("package_ready", root / "tools/jetson/package-ready.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest, seeds = module.builtin(root)
    key = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    base = runtime / ".portable-launch"
    if base.is_symlink() or getattr(base, "is_junction", lambda: False)():
        raise ValueError("Launch metadata cannot traverse links")
    base.mkdir(exist_ok=True, mode=0o700)
    target = base / key
    if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
        raise ValueError("Launch metadata cannot use links")
    if not target.exists():
        # No installed state is overwritten. Interrupted writes are not reused.
        pending = Path(tempfile.mkdtemp(prefix=".prepare-", dir=base))
        try:
            module.write_builtin(root, pending / "builtin")
            os.rename(pending / "builtin", target)
        finally:
            shutil.rmtree(pending)
    if json.loads(module.checked(target, "manifest.json").read_text()) != manifest:
        raise ValueError("Existing launch metadata differs from the program")
    for name, content in seeds.items():
        if module.checked(target, "files/" + name).read_bytes() != content:
            raise ValueError("Existing launch preset differs from the program")
    return key


if __name__ == "__main__":
    print(prepare(Path("/workspace/tuber"), Path("/workspace/tuber/runtime")))
