"""Path, endpoint, and credential boundary validators with zero domain dependencies."""

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def validate_api_key(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise ValueError("API key格式无效")


def endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("API Base URL无效")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ValueError("API Base URL端口或地址无效") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 33 for char in value)
    ):
        raise ValueError("API Base URL须为HTTP(S)，凭证须单独填写，不接受查询串或用户名密码")
    if parsed.scheme == "http":
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
            local = (
                address.is_private
                or address.is_loopback
                or any(address in network for network in _TAILSCALE_NETWORKS)
            )
        except ValueError:
            local = host in {"localhost", "host.docker.internal"} or host.endswith(".local")
            if not local and "." not in host and host.replace("-", "").isalnum():
                managed = urlsplit(os.getenv("OMNI_GATEWAY_URL", ""))
                local = managed.scheme == "http" and (host, parsed.port) == (managed.hostname, managed.port)
        if not local:
            raise ValueError("公网API须使用HTTPS；本地服务可使用HTTP")
    return value.rstrip("/")


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
