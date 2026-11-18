from pathlib import Path


def project_wav(root, value, *, runtime=None):
    """Resolve an existing WAV without following links or escaping fixed assets."""
    if not isinstance(value, str) or not value or "://" in value:
        raise ValueError("Reference must be a configured project WAV")
    root = root.resolve()
    runtime_root = Path(runtime).resolve() if runtime is not None else root / "runtime"
    raw = Path(value)
    if ".." in raw.parts:
        raise ValueError("Reference traversal is disabled")
    if runtime is not None and raw.parts[:3] == ("runtime", "tts", "ref"):
        path = runtime_root / Path(*raw.parts[1:])
    else:
        path = raw if raw.is_absolute() else root / raw
    if not path.is_relative_to(root) and not path.is_relative_to(runtime_root):
        raise ValueError("Reference must stay inside this project")
    for component in [path, *path.parents]:
        if component in {root, runtime_root}:
            break
        if component.is_symlink():
            raise ValueError("Reference links are disabled")
    path = path.resolve(strict=True)
    allowed = (root / "assets", runtime_root / "tts/ref")
    if (
        not any(path.is_relative_to(base) for base in allowed)
        or path.suffix.lower() != ".wav"
        or not path.is_file()
    ):
        raise ValueError("Reference must be an existing project WAV")
    with path.open("rb") as audio:
        header = audio.read(12)
    if header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
        raise ValueError("Reference must be a WAV file")
    return path
