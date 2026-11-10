"""Catch application/TTS pin conflicts before the native ARM64 image build."""

import tomllib
from pathlib import Path

from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).parents[1]
JETSON = default_environment() | {
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "sys_platform": "linux",
    "os_name": "posix",
    "platform_machine": "aarch64",
    "platform_system": "Linux",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "extra": "",
}


def test_jetson_tts_pins_accept_locked_application_dependencies():
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        canonicalize_name(package["name"]): package
        for package in lock["package"]
        if not package.get("resolution-markers")
        or any(Marker(marker).evaluate(JETSON) for marker in package["resolution-markers"])
    }
    # Only production dependencies are exported by the Dockerfile, not dev tools.
    pending = ["tuber-studio"]
    application = {}
    while pending:
        name = pending.pop()
        if name in application:
            continue
        package = packages[name]
        application[name] = package["version"]
        pending.extend(
            canonicalize_name(dependency["name"])
            for dependency in package.get("dependencies", [])
            if not dependency.get("marker") or Marker(dependency["marker"]).evaluate(JETSON)
        )

    shared = set()
    for line in (ROOT / "tools/jetson/tts/requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        if name not in application or (requirement.marker and not requirement.marker.evaluate(JETSON)):
            continue
        shared.add(name)
        assert application[name] in requirement.specifier, (
            f"TTS {requirement} conflicts with uv.lock {name}=={application[name]}"
        )
    assert "tzdata" in shared
