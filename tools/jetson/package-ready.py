"""Collect only the verified Mini128K/Yaoyao assets for an offline image build.

Run with Python's standard library; no CUDA, downloads, credentials or database
is loaded. Output is a new Docker context under the project's ignored tmp/.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import runpy
import shutil
from pathlib import Path

VOICE = 'yaoyao_sovits_v2'
CANONICAL = Path('/workspace/tuber/runtime')
SOURCE_FILES = {
    't2s': 'tts/weights/yaoyao-s1-sanitized.ckpt',
    'vits': 'tts/weights/yaoyao-s2-sanitized.pth',
    'reference': 'tts/ref/yaoyao-reference32k.wav',
    'bert/config.json': 'tts/pretrained/chinese-roberta-wwm-ext-large/config.json',
    'bert/tokenizer.json': 'tts/pretrained/chinese-roberta-wwm-ext-large/tokenizer.json',
    'bert/pytorch_model.bin': 'tts/pretrained/chinese-roberta-wwm-ext-large/pytorch_model.bin',
    'hubert/config.json': 'tts/pretrained/chinese-hubert-base/config.json',
    'hubert/preprocessor_config.json': 'tts/pretrained/chinese-hubert-base/preprocessor_config.json',
    'hubert/pytorch_model.bin': 'tts/pretrained/chinese-hubert-base/pytorch_model.bin',
}


def checked(base: Path, relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or raw.anchor or '..' in raw.parts or '\\' in relative:
        raise ValueError('Asset path must be an explicit relative filename')
    path = base / raw
    for item in (path, *path.parents):
        if item == base:
            break
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('Asset links are disabled')
    path = path.resolve(strict=True)
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError('Asset escaped its fixed directory')
    return path


def hash_file(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024 * 1024):
            result.update(block)
        if hasattr(os, 'posix_fadvise'):
            os.posix_fadvise(source.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    return result.hexdigest()


def read_json(path: Path):
    if path.stat().st_size > 8_000_000:
        raise ValueError('Metadata is unexpectedly large')
    return json.loads(path.read_text(encoding='utf-8'))


def bundle_contract(root):
    spec = importlib.util.spec_from_file_location('ready_genie_contract', root / 'tools/jetson/tts/genie_bundle.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect(root: Path, runtime: Path) -> dict[str, str | None]:
    """Return an exact runtime whitelist plus expected hashes where available."""
    definitions = runpy.run_path(str(root / 'tools/jetson/initialize-data.py'))
    llm = definitions['LLM_ARTIFACTS']['llm2b']
    selected = {'models/MiniCPM5-2B-Q4_K_M.gguf': llm['sha256']}
    if checked(runtime, next(iter(selected))).stat().st_size != llm['size']:
        raise ValueError('MiniCPM5-2B GGUF size differs')
    directory = 'tts/genie/' + VOICE
    bundle = read_json(checked(runtime, directory + '/manifest.json'))
    metadata = read_json(checked(runtime, 'tts/reference_features/' + VOICE + '/metadata.json'))
    identity = metadata['identity']
    if bundle['voice_id'] != VOICE or identity != bundle['source_identity']:
        raise ValueError('Selected reference and Genie source identity differ')
    contract = bundle_contract(root)
    if bundle.get('bundle_identity') != contract._manifest_identity(bundle):
        raise ValueError('Genie manifest identity is invalid')
    if set(bundle.get('implementations', {})) != set(contract.IMPLEMENTATIONS):
        raise ValueError('Genie implementation identity set differs')
    for name, record in bundle['implementations'].items():
        implementation = checked(root, 'tools/jetson/tts/' + name)
        if implementation.stat().st_size != record['bytes'] or hash_file(implementation) != record['sha256']:
            raise ValueError('Prepared Genie implementation differs from the image source')
    if set(identity['paths']) != set(SOURCE_FILES) or set(identity['sha256']) != set(SOURCE_FILES):
        raise ValueError('Unexpected original-source dependency set')
    for name, relative in SOURCE_FILES.items():
        if identity['paths'][name] != str(CANONICAL / relative):
            raise ValueError('Source path differs from the canonical runtime layout')
        selected[relative] = identity['sha256'][name]
    selected['tts/tts_infer.yaml'] = identity['config_sha256']
    for key, filename in (('metadata', 'metadata.json'), ('features', 'features.npz')):
        relative = 'tts/reference_features/' + VOICE + '/' + filename
        record = bundle['reference'][key + '_identity']
        if bundle['reference'][key] != relative or checked(runtime, relative).stat().st_size != record['bytes']:
            raise ValueError('Genie reference file identity differs')
        selected[relative] = record['sha256']
    if selected['tts/reference_features/' + VOICE + '/features.npz'] != metadata['npz_sha256']:
        raise ValueError('Reference NPZ identity differs')
    selected[directory + '/manifest.json'] = None
    for name, record in bundle['files'].items():
        if name == 'manifest.json' or Path(name).parts[0] not in ('ar', 'bert', 'vits', 'encoder.onnx'):
            raise ValueError('Unexpected permanent Genie artifact')
        relative = directory + '/' + name
        if checked(runtime, relative).stat().st_size != record['bytes']:
            raise ValueError('Genie artifact size differs')
        selected[relative] = record['sha256']
    for relative, members in definitions['NLTK_PACKAGES'].items():
        archive = 'nltk_data/' + relative + '.zip'
        if not definitions['valid_archive'](checked(runtime, archive), members):
            raise ValueError('NLTK consumer archive is invalid')
        selected[archive] = None
        for member, size in members.items():
            filename = 'nltk_data/' + str(Path(relative).parent / member).replace('\\', '/')
            if checked(runtime, filename).stat().st_size != size:
                raise ValueError('NLTK consumer file is missing or incomplete')
            selected[filename] = None
    for relative in selected:
        if any(part in {'provider-credentials.json', 'admin-token', 'overlay-token', 'inference-token'}
               or part.endswith(('.sqlite3', '.log', '.pem', '.key')) for part in Path(relative).parts):
            raise ValueError('Private or stateful data cannot be included')
    return selected


def package(root: Path, runtime: Path, output: Path) -> dict:
    root, runtime, output = root.resolve(), runtime.resolve(), output.absolute()
    if not output.is_relative_to(root / 'tmp') or output.exists():
        raise ValueError('Use a new build context inside this project tmp/')
    if output.resolve() != output:
        raise ValueError('Build output cannot traverse links')
    selected = collect(root, runtime)
    files = output / 'builtin/files'
    files.mkdir(parents=True)
    records = []
    for relative, expected in selected.items():
        source = checked(runtime, relative)
        before = source.stat()
        target = files / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        digest = hash_file(target)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or expected is not None and digest != expected:
            raise ValueError('A source changed or did not match its verified content: ' + relative)
        mode = 0o600 if relative == 'tts/tts_infer.yaml' else 0o444
        records.append({'path': relative, 'bytes': target.stat().st_size, 'sha256': digest, 'mode': mode,
                        **({'policy': 'preserve'} if relative == 'tts/tts_infer.yaml' else {})})
        print(json.dumps({'packed': relative, 'bytes': target.stat().st_size}), flush=True)
    preset = read_json(root / 'config/studio.example.json')
    preset['features']['live_enabled'] = True
    preset['inference_broker']['mode'] = 'live'
    preset['tts'].update(voice_id=VOICE, local_backend='genie_onnx_int8')
    target = files / 'studio.json'
    target.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding='utf-8')
    records.append({'path': 'studio.json', 'bytes': target.stat().st_size, 'sha256': hash_file(target),
                    'mode': 0o600, 'policy': 'preserve'})
    manifest = {'format': 1, 'files': sorted(records, key=lambda item: item['path'])}
    (output / 'builtin/manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    shutil.copyfile(root / 'tools/jetson/run-ready.sh', output / 'run-ready.sh')
    return {'files': len(records), 'bytes': sum(item['bytes'] for item in records), 'output': str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    print(json.dumps(package(root, args.runtime or root / 'runtime', args.output)), flush=True)


if __name__ == '__main__':
    main()
