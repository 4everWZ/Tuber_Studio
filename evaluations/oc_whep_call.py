"""Record the two approved OC calls through the deployed Studio and workstation gateway."""

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

from evaluations.private_rig_call import ROOT, connection, envelope, exercise, pcm8

CASES = {
    "original": (
        "oc-mature-v1",
        "大肥鱼-动漫通话头像.png",
        "682c1fddb63335e49983f35731b9a35e4b2a18a5277fc2db677c15e54446985f",
    ),
    "q": (
        "oc-q-v1",
        "大肥鱼-Q版通话头像.png",
        "3c3b3cbf6cff675061a9b9a15635562e7f22d02daf23ad8f50d84b7f963bdd1a",
    ),
}


def remote_origin(url):
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if (
        parts.scheme not in {"https", "http"}
        or not host
        or parts.username
        or parts.password
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or host.lower().rstrip(".").endswith("localhost")
        or address
        and (address.is_loopback or address.is_unspecified)
    ):
        raise ValueError("Expected a workstation origin, not a local service or URL path")
    return url.rstrip("/")


async def run(args):
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            raise RuntimeError(f"Missing {executable}")
    subprocess.run([str(args.media_python), "-c", "import av, cv2, numpy, PIL"], check=True)
    reference = envelope(pcm8(args.wav))
    for _, filename, digest in CASES.values():
        if hashlib.sha256((args.portraits / filename).read_bytes()).hexdigest() != digest:
            raise ValueError("Expected the approved OC source")
    async with async_playwright() as playwright:
        executable = os.getenv("CHROMIUM_EXECUTABLE") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            raise RuntimeError("Set CHROMIUM_EXECUTABLE to the installed Chromium binary")
        if not args.worker_switched:
            print("Local prerequisites OK. No gateway requests or calls.")
            return
        service = connection(os.environ)
        if not service or not args.studio or not args.output:
            raise ValueError("Set the ordinary gateway credential, --studio and a fresh --output directory")
        gateway, token = service
        gateway, origin = remote_origin(gateway), remote_origin(args.studio)
        if not origin.startswith("https://"):
            raise ValueError("Workstation microphone capture needs a secure Studio origin")
        async with httpx.AsyncClient(
            base_url=gateway, headers={"Authorization": "Bearer " + token}, trust_env=False
        ) as client:
            for identity, _, _ in CASES.values():
                view = (await client.get("/v1/avatar/avatars/" + identity)).raise_for_status().json()
                if (view.get("id"), view.get("driver"), view.get("state")) != (identity, "inochi2d", "ready"):
                    raise RuntimeError("Both exact OC registrations must be ready")
        args.output.mkdir(parents=True, exist_ok=False)
        (ROOT / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="oc-mic-") as temporary:
            microphone = Path(temporary) / "microphone.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-i",
                    str(args.wav),
                    "-af",
                    "adelay=6000:all=1,apad=pad_dur=45",
                    "-ac",
                    "1",
                    "-ar",
                    "44100",
                    str(microphone),
                ],
                check=True,
            )
            browser = await playwright.chromium.launch(
                executable_path=executable,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith(("TUBER_", "OMNI_", "OC_QA_"))
                },
                args=[
                    "--no-proxy-server",
                    "--use-fake-device-for-media-stream",
                    "--use-fake-ui-for-media-stream",
                    f"--use-file-for-fake-audio-capture={microphone}",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            failures = []
            try:
                for profile, (identity, filename, _) in CASES.items():
                    work = args.output / identity
                    work.mkdir()
                    try:
                        result = await exercise(
                            browser,
                            origin,
                            profile,
                            work,
                            gateway,
                            token,
                            reference,
                            args.media_python,
                            oc_id=identity,
                            storage_state=args.storage_state,
                            portrait=args.portraits / filename,
                        )
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-nostdin",
                                "-v",
                                "error",
                                "-n",
                                "-i",
                                str(work / "received.webm"),
                                "-map",
                                "0:v",
                                "-map",
                                "0:a",
                                "-fps_mode",
                                "passthrough",
                                "-c:v",
                                "libx264",
                                "-crf",
                                "19",
                                "-pix_fmt",
                                "yuv420p",
                                "-c:a",
                                "aac",
                                "-movflags",
                                "+faststart",
                                str(work / "call.mp4"),
                            ],
                            check=True,
                        )
                        offset = result["first_open_minus_voice_ms"]
                        result["onset_within_160_ms"] = offset is not None and abs(offset) <= 160
                        result["passed"] = result["passed"] and result["onset_within_160_ms"]
                        result["manual_chinese_and_identity_review"] = "pending"
                    except Exception as error:
                        result = {"passed": False, "error_type": type(error).__name__}
                        result["stage"] = [
                            entry.lineno
                            for entry in traceback.extract_tb(error.__traceback__)
                            if Path(entry.filename).name == "private_rig_call.py"
                        ]
                        print(identity, result, str(error).splitlines()[0].split("?")[0][:160], flush=True)
                    (work / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                    if not result["passed"]:
                        failures.append(identity)
            finally:
                await browser.close()
            if failures:
                raise RuntimeError("No automatic pass: " + ", ".join(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-switched", action="store_true", help="Only after explicit cutover notice")
    parser.add_argument("--studio", help="Deployed workstation Studio HTTPS origin")
    parser.add_argument("--storage-state", type=Path, help="Private Playwright Studio login state")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--portraits", type=Path, default=ROOT / "runtime/private-artifacts")
    parser.add_argument("--wav", type=Path, default=ROOT / "assets/whale/sample1.wav")
    parser.add_argument(
        "--media-python",
        type=Path,
        default=ROOT.parent
        / "omni-avatar-api/.venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
    )
    asyncio.run(run(parser.parse_args()))
