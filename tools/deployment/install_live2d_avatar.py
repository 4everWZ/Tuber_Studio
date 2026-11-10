"""Preflight and install a locally authorized Cubism Editor runtime export."""

import argparse
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from tuber_studio.live.avatar import SDK, avatar_package, checked_file, load_object


def preflight(package: Path, runtime: Path, character: str, *, install: bool = False):
    if not re.fullmatch(r"[a-z0-9_-]{1,50}", character):
        raise ValueError("角色 ID 格式无效")
    package = package.resolve()
    runtime = runtime.resolve()
    manifest, files = avatar_package(package)
    model = load_object(checked_file(package, manifest.model), 256 * 1024)
    moc = (PurePosixPath(manifest.model).parent / model["FileReferences"]["Moc"]).as_posix()
    with checked_file(package, moc).open("rb") as source:
        if source.read(4) != b"MOC3":
            raise ValueError("运行文件缺少 MOC3 标记。请从 Cubism Editor 导出。")
    root = runtime / "live2d"
    missing_sdk = []
    for name in SDK:
        try:
            checked_file(root / "sdk", name)
        except ValueError:
            missing_sdk.append(name)
    target = root / character
    if install:
        if root.resolve() != root:
            raise ValueError("Live2D 安装目录不能重定向")
        if missing_sdk:
            raise ValueError("请先安装授权的 Live2D SDK 文件 " + "、".join(missing_sdk))
        if target.exists() or target.is_symlink():
            raise ValueError("此角色已有 Live2D 目录。请先移出备份。")
        with TemporaryDirectory(prefix=".avatar-stage-", dir=root) as staging:
            candidate = Path(staging) / character
            for item in sorted(files | {"avatar.json"}):
                destination = candidate / item
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checked_file(package, item), destination)
            avatar_package(candidate)
            candidate.rename(target)
    return {
        "model": manifest.model,
        "files": len(files) + 1,
        "sdk_missing": missing_sdk,
        "installed": install,
        "target": str(target),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="含 avatar.json 的 Cubism 导出目录")
    parser.add_argument("--runtime", type=Path, required=True, help="Tuber Studio 的本机运行目录")
    parser.add_argument("--character", default="whale")
    parser.add_argument("--install", action="store_true", help="将通过预检的模型文件复制到角色目录")
    args = parser.parse_args()
    try:
        result = preflight(args.package, args.runtime, args.character, install=args.install)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        parser.exit(1, f"Live2D 预检失败：{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
