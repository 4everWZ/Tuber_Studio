"""The offline image collector must not copy the operator's runtime wholesale."""
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    'ready_assets', Path(__file__).parents[1] / 'tools/jetson/package-ready.py'
)
assets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(assets)


@pytest.fixture
def payload(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    runtime = root / 'runtime'

    def put(relative, data=b'fixed model bytes'):
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    hashes = {key: put(relative) for key, relative in assets.SOURCE_FILES.items()}
    yaml_hash = put('tts/tts_infer.yaml', b'custom: fixed original YAML\n')
    npz_hash = put('tts/reference_features/' + assets.VOICE + '/features.npz')
    identity = {'paths': {k: str(assets.CANONICAL / v) for k, v in assets.SOURCE_FILES.items()},
                'sha256': hashes, 'config_sha256': yaml_hash}
    put('tts/reference_features/' + assets.VOICE + '/metadata.json',
        json.dumps({'identity': identity, 'npz_sha256': npz_hash}).encode())
    graph_hash = put('tts/genie/' + assets.VOICE + '/encoder.onnx')
    bundle = {'voice_id': assets.VOICE, 'source_identity': identity,
              'files': {'encoder.onnx': {'sha256': graph_hash, 'bytes': len(b'fixed model bytes')}}}
    gguf_hash = put('models/MiniCPM5-2B-Q4_K_M.gguf')
    monkeypatch.setattr(assets.runpy, 'run_path', lambda _: {
        'LLM_ARTIFACTS': {'llm2b': {'size': len(b'fixed model bytes'), 'sha256': gguf_hash}},
        'NLTK_PACKAGES': {},
    })
    (root / 'config').mkdir()
    (root / 'config/studio.example.json').write_text(json.dumps({
        'features': {'live_enabled': False}, 'inference_broker': {'mode': 'text'},
        'tts': {'kind': 'gpt_sovits', 'voice_id': 'old'},
    }))
    (root / 'tools/jetson').mkdir(parents=True)
    (root / 'tools/jetson/run-ready.sh').write_text('#!/bin/sh\n')
    originals = Path(__file__).parents[1] / 'tools/jetson/tts'
    (root / 'tools/jetson/tts').mkdir()
    for filename in ('genie_backend.py', 'genie_sampling.py', 'genie_bert.py', 'genie_bundle.py',
                     'genie_quantize.py', 'compact.py', 'serve.py', 'inference_only.py'):
        shutil.copyfile(originals / filename, root / 'tools/jetson/tts' / filename)
    contract = assets.bundle_contract(root)
    bundle['implementations'] = {name: {'bytes': (originals / name).stat().st_size,
                                      'sha256': assets.hash_file(originals / name)}
                                 for name in contract.IMPLEMENTATIONS}
    bundle['reference'] = {}
    for key, filename in (('metadata', 'metadata.json'), ('features', 'features.npz')):
        relative = 'tts/reference_features/' + assets.VOICE + '/' + filename
        path = runtime / relative
        bundle['reference'][key] = 'runtime/' + relative
        bundle['reference'][key + '_identity'] = {'bytes': path.stat().st_size, 'sha256': assets.hash_file(path)}
    bundle['bundle_identity'] = contract._manifest_identity(bundle)
    put('tts/genie/' + assets.VOICE + '/manifest.json', json.dumps(bundle).encode())
    for private in ('admin-token', 'overlay-token', 'inference-token', 'provider-credentials.json',
                    'studio.sqlite3', 'inference.log', 'evaluations/experiment.py', 'studio.json'):
        put(private, b'DO-NOT-PACK-private-operator-state')
    return root, runtime


def test_exact_assets_and_clean_preset_exclude_operator_state(payload):
    root, runtime = payload
    output = root / 'tmp/build'
    assets.package(root, runtime, output)
    manifest = json.loads((output / 'builtin/manifest.json').read_text())
    entries = {item['path']: item for item in manifest['files']}
    assert set(entries) == {*assets.collect(root, runtime), 'studio.json'}
    assert {name for name, item in entries.items() if item.get('policy') == 'preserve'} == {
        'studio.json', 'tts/tts_infer.yaml'
    }
    preset = json.loads((output / 'builtin/files/studio.json').read_text())
    assert preset['tts']['voice_id'] == assets.VOICE
    assert preset['tts']['local_backend'] == 'genie_onnx_int8'
    assert preset['inference_broker']['mode'] == 'live'
    for path in (output / 'builtin/files').rglob('*'):
        if path.is_file():
            assert b'DO-NOT-PACK' not in path.read_bytes()


def test_reference_metadata_cannot_select_a_private_file(payload):
    root, runtime = payload
    path = runtime / 'tts/reference_features' / assets.VOICE / 'metadata.json'
    data = json.loads(path.read_text())
    data['identity']['paths']['reference'] = str(assets.CANONICAL / 'admin-token')
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        assets.collect(root, runtime)


def test_changed_model_bytes_and_existing_output_are_rejected(payload):
    root, runtime = payload
    (runtime / 'tts/ref/yaoyao-reference32k.wav').write_bytes(b'corrupted reference')
    with pytest.raises(ValueError, match='verified content'):
        assets.package(root, runtime, root / 'tmp/corrupt')
    output = root / 'tmp/existing'
    output.mkdir()
    (output / 'keep').write_text('unrelated')
    with pytest.raises(ValueError, match='new build context'):
        assets.package(root, runtime, output)
    assert (output / 'keep').read_text() == 'unrelated'


def test_source_symlink_cannot_escape_whitelist(payload, tmp_path):
    root, runtime = payload
    source = runtime / 'tts/ref/yaoyao-reference32k.wav'
    source.unlink()
    outside = tmp_path / 'private.wav'
    outside.write_bytes(b'private data')
    try:
        source.symlink_to(outside)
    except OSError:
        pytest.skip('Unprivileged symbolic links unavailable')
    with pytest.raises(ValueError, match='links'):
        assets.package(root, runtime, root / 'tmp/linked-source')


@pytest.mark.parametrize('change', ['manifest', 'implementation'])
def test_stale_bundle_or_runtime_code_is_rejected_before_build(payload, change):
    root, runtime = payload
    if change == 'manifest':
        path = runtime / 'tts/genie' / assets.VOICE / 'manifest.json'
        data = json.loads(path.read_text())
        data['bundle_identity'] = '0' * 64
        path.write_text(json.dumps(data))
    else:
        path = root / 'tools/jetson/tts/serve.py'
        path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='manifest identity|image source'):
        assets.collect(root, runtime)
