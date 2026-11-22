"""Exercise real Omni speech through an isolated Studio and two original Inochi2D WHEP rigs."""

import argparse
import asyncio
import base64
import json
import math
import os
import secrets
import socket
import statistics
import subprocess
import tempfile
from array import array
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from playwright.async_api import async_playwright

from evaluations.agent.omni_chat_loop import ROOT, connection
from evaluations.browser_login import fill_private_login
from tuber_studio.app import create_app
from tuber_studio.core.config import Settings

MOUTH_CENTERS = {"original": (370, 418), "q": (347, 465), "oc-mature-v1": (334, 427), "oc-q-v1": (328, 473)}


def envelope(raw):
    values = array("h")
    values.frombytes(raw)
    return [
        math.sqrt(sum(v * v for v in values[i : i + 160]) / 160) for i in range(0, len(values) - 159, 160)
    ]


def pcm8(path):
    return subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-ar",
            "8000",
            "-ac",
            "1",
            "-f",
            "s16le",
            "pipe:1",
        ]
    )


def pcm_envelope(raw, rate):
    samples = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "pipe:1",
        ],
        input=raw,
        capture_output=True,
        check=True,
    ).stdout
    return envelope(samples)


def match_audio(signal, ref):
    # Real model chunks can arrive with intervening silence on the WHEP timeline.
    # Compare each chunk separately, irrespective of receive offset and Opus gain.
    ref = ref[:125]
    if len(ref) < 30 or len(signal) < len(ref):
        return 0
    mean = statistics.mean(ref)
    ref = [v - mean for v in ref]
    energy = sum(v * v for v in ref)
    best = 0
    for i in range(len(signal) - len(ref) + 1):
        segment = signal[i : i + len(ref)]
        var = sum(v * v for v in segment) - sum(segment) ** 2 / len(ref)
        if var > 0 and energy:
            best = max(best, sum(a * b for a, b in zip(ref, segment)) / math.sqrt(energy * var))
    return round(best, 3)


async def exercise(
    browser,
    origin,
    profile,
    work,
    gateway,
    token,
    reference,
    media_python,
    *,
    oc_id=None,
    storage_state=None,
    portrait=None,
    window=None,
):
    mouth_center = MOUTH_CENTERS[oc_id or profile]
    ctx = await browser.new_context(
        service_workers="block", permissions=["microphone"], storage_state=storage_state
    )
    diagnostics = []
    try:
        if window:
            from evaluations.live.oc_call_window import INIT, measure

            await ctx.expose_function("collectWhepDiagnostic", lambda row: diagnostics.append(row))
            await ctx.add_init_script(path=INIT)
        page = await ctx.new_page()
        await page.goto(origin)
        if oc_id and os.environ.get("OC_QA_LOGIN_TOKEN"):
            if page.url.rstrip("/") != origin or not origin.startswith("https://"):
                raise RuntimeError("Unexpected Studio login origin")
            await fill_private_login(page, os.environ["OC_QA_LOGIN_TOKEN"])
            await page.locator("#login-form button[type=submit]").click()
        await page.locator("#operator-chat-more-toggle").click()
        await page.locator("#private-call-open").click()
        choices = await page.evaluate("""async () => {
            const response = await fetch('/api/chat/private-call/avatars');
            if (!response.ok) throw Error('Profile lookup: HTTP ' + response.status);
            return response.json();
        }""")
        if profile not in choices.get("profiles", []):
            raise RuntimeError("Studio did not offer the requested profile")
        await page.get_by_role("combobox", name="通话形象", exact=True).select_option(profile)
        packets, rates, acknowledgements = defaultdict(list), {}, {}
        state = {
            "native": False,
            "input_seconds": 0,
            "speech_input_packets": 0,
            "whep_201": 0,
            "avatar_ack_ms": 0,
            "bound_source": False,
            "ticket_200": False,
        }

        def observe_response(r):
            if r.url.endswith("/whep") and r.request.method == "POST" and r.status == 201:
                state["whep_201"] += 1
            if r.url.endswith("/private-call/ticket") and r.status == 200:
                state["ticket_200"] = True

        page.on("response", observe_response)
        source_id = None
        input_pcm = bytearray()
        responding_to_speech = set()

        def socket_open(ws):
            if "/v1/realtime?" in ws.url:

                def received(raw):
                    nonlocal source_id
                    e = json.loads(raw)
                    if e["type"] == "session.created":
                        state["realtime_model"] = e["session"].get("model")
                        state["native"] = (
                            e["session"].get("capabilities", {}).get("implementation_level")
                            == "model_native_duplex"
                        )
                    elif e["type"] == "gateway.session.created":
                        source_id = e.get("source_session_id")
                        state["bound_source"] = bool(e.get("source_session_id") and e.get("avatar_available"))
                    elif e["type"] in ("response.audio.delta", "response.output_audio.delta"):
                        rid = e["response_id"]
                        if rid not in packets and state["speech_input_packets"]:
                            responding_to_speech.add(rid)
                        rates[rid] = e.get("sample_rate_hz", 24000)
                        packets[rid].append(base64.b64decode(e["delta"]))

                def sent(raw):
                    e = json.loads(raw)
                    if e["type"] == "input_audio_buffer.append":
                        pcm = base64.b64decode(e["audio"])
                        state["input_seconds"] += len(pcm) / (2 * e["sample_rate_hz"])
                        state["speech_input_packets"] += int(any(pcm))
                        input_pcm.extend(pcm)
                    elif e["type"] == "playback.ack":
                        acknowledgements[e["response_id"]] = e["played_ms"]

                ws.on("framereceived", received)
                ws.on("framesent", sent)
            elif "/events?" in ws.url:

                def event_ack(raw):
                    e = json.loads(raw)
                    if e["type"] == "playback.ack":
                        state["avatar_ack_ms"] = max(state["avatar_ack_ms"], e["played_ms"])

                ws.on("framesent", event_ack)

        page.on("websocket", socket_open)
        try:
            async with page.expect_response(
                lambda r: r.url.endswith("/api/chat/private-call/avatars") and r.request.method == "POST"
            ) as attachment:
                await page.locator("#private-call-start").click()
            bound = await attachment.value
            assert bound.ok, f"Avatar bind HTTP {bound.status}"
            joined = (await bound.json())["handle"].split(".", 1)[0]
            if oc_id:
                async with httpx.AsyncClient(
                    base_url=gateway, headers={"Authorization": "Bearer " + token}, trust_env=False
                ) as client:
                    actual = (await client.get("/v1/avatar/sessions/" + joined)).raise_for_status().json()
                if actual["avatar_id"] != oc_id or actual["format"]["driver"] != "inochi2d":
                    raise RuntimeError("Studio selected a different rig; not an OC recording")
            await page.get_by_text("麦克风已开启", exact=False).wait_for(timeout=35000)
        except Exception:
            print("Handshake:", await page.locator("#private-call-state").inner_text(), state, flush=True)
            raise
        await page.wait_for_function(
            """() => {const v=document.querySelector('#private-call-dialog video');
            return v.videoWidth===640 && v.videoHeight===720 && v.srcObject?.getAudioTracks().length===1 && v.currentTime>.2}""",
            timeout=35000,
        )
        if not await page.get_by_label("实时通话形象").is_visible():
            raise RuntimeError("The WHEP player is hidden")
        if window:
            state["source_verified"] = bool(source_id and actual.get("source_session_id") == source_id)
            return await measure(page, work, gateway, token, joined, state, *window)
        await page.evaluate(
            Path(__file__).with_name("private_rig_capture.js").read_text(encoding="utf-8-sig"), mouth_center
        )

        for _ in range(110):
            await asyncio.sleep(0.25)
            if any(
                k in responding_to_speech
                and sum(map(len, v)) / 2 / rates[k] > 2.5
                and acknowledgements.get(k, 0) > 2400
                for k, v in packets.items()
            ):
                break
        await asyncio.sleep(1.5)
        recorded = await page.evaluate("finishProbe()")
        capture = work / "received.webm"
        capture.write_bytes(base64.b64decode(recorded["audio"]))
        async with httpx.AsyncClient(
            base_url=gateway, headers={"Authorization": "Bearer " + token}, trust_env=False
        ) as client:
            view = (await client.get("/v1/avatar/sessions/" + joined)).raise_for_status().json()
            state["server_bound"] = bool(
                source_id
                and view["source_session_id"] == source_id
                and view["avatar_id"] == (oc_id or ("regular" if profile == "original" else "q"))
                and view["format"]["driver"] == "inochi2d"
            )
            state["accepted_avatar_ack_ms"] = view["playback"].get("played_ms", 0)
            state["avatar_id"] = view["avatar_id"]
            state["driver"] = view["format"]["driver"]
            if not oc_id:
                state["sender_frame_age_ms"] = (
                    (await client.get("/qa/sender-clocks/" + joined)).raise_for_status().json()
                )
            await page.locator("#private-call-end").click()
            async with asyncio.timeout(5):
                while True:
                    view = (await client.get("/v1/avatar/sessions/" + joined)).raise_for_status().json()
                    if view["state"] == "closed":
                        state["hangup_state"] = view["state"]
                        break
                    await asyncio.sleep(0.1)
        synchronized = json.loads(
            subprocess.check_output(
                [
                    str(media_python),
                    str(Path(__file__).with_name("private_rig_recording.py")),
                    str(capture),
                    *map(str, mouth_center),
                    *([str(portrait)] if portrait else []),
                ]
            )
        )
        received = envelope(pcm8(capture))
        input_score = match_audio(pcm_envelope(input_pcm, 16000), reference)
        matches = []
        for key in responding_to_speech:
            chunks = packets[key]
            scores = [match_audio(received, pcm_envelope(pcm, rates[key])) for pcm in chunks[:3] if any(pcm)]
            if len(scores) >= 2:
                matches.append((statistics.median(scores), key))
        correlation, rid = max(matches, default=(0, None))
        points = recorded["points"]
        quiet = [p[2] for p in points if p[1] < 0.004]
        loud = [p[2] for p in points if p[1] > 0.025]

        def median(values):
            return round(statistics.median(values), 1) if values else None

        report = (
            state
            | synchronized
            | {
                "input_envelope_r": input_score,
                "decoded_sizes": recorded["sizes"],
                "peak_mouth_area_px": max((p[2] for p in points), default=0),
                "profile": profile,
                "response_pcm_ms": round(sum(map(len, packets[rid])) * 500 / rates[rid]) if rid else 0,
                "response_ack_ms": acknowledgements.get(rid, 0),
                "peak_model_rms": round(
                    max(pcm_envelope(b"".join(packets[rid]), rates[rid]), default=0) / 32768, 3
                )
                if rid
                else 0,
                "pcm_to_received_envelope_r": correlation,
                "frames": recorded["frames"],
                "playing": recorded["playing"],
                "quiet_frames": len(quiet),
                "voiced_frames": len(loud),
                "quiet_mouth_area_px": median(quiet),
                "voiced_mouth_area_px": median(loud),
            }
        )
        report["passed"] = bool(
            all(
                (
                    state["native"],
                    state["ticket_200"],
                    state["server_bound"],
                    state["accepted_avatar_ack_ms"],
                    state["bound_source"],
                    state["speech_input_packets"],
                    state["whep_201"],
                    state["avatar_ack_ms"],
                    report["playing"],
                    report["response_ack_ms"] > 0,
                    input_score > 0.75,
                    correlation > 0.85,
                    len(quiet) >= 4,
                    len(loud) >= 4,
                )
            )
            and synchronized["recorded_voiced_frames"] >= 4
            and synchronized["recorded_quiet_frames"] >= 4
            and synchronized["recorded_voiced_area_px"] > synchronized["recorded_quiet_area_px"] + 20
            and median(loud) > median(quiet) + 20
        )
        return report
    finally:
        try:
            if oc_id and ctx.pages:
                end = ctx.pages[0].locator("#private-call-end")
                if await end.is_visible() and await end.is_enabled():
                    await end.click(timeout=3000)
        finally:
            try:
                await ctx.close()
            finally:
                if window:
                    (work / "whep-diagnostics.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")


@asynccontextmanager
async def isolated_gateway(repo, python, rigs, upstream, token):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNI_", "TUBER_"))}
    env.update(
        PYTHONPATH=str(repo),
        QA_LOCAL_TOKEN=token,
        QA_RIG_DIR=str(rigs),
        OMNI_GATEWAY_URL=upstream[0],
        OMNI_STUDIO_TOKEN=upstream[1],
    )
    proc = await asyncio.create_subprocess_exec(
        str(python),
        str(Path(__file__).with_name("private_rig_gateway.py")),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        port = int(await asyncio.wait_for(proc.stdout.readline(), 15))
        yield f"http://127.0.0.1:{port}"
    finally:
        if proc.returncode is None:
            proc.stdin.write(b"\n")
            await proc.stdin.drain()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()


async def run(args, work, service):
    media_python = args.media_python or args.gateway_repo / ".venv" / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    token = secrets.token_urlsafe(32)
    async with AsyncExitStack() as stack:
        gateway = await stack.enter_async_context(
            isolated_gateway(
                args.gateway_repo.resolve(), media_python, args.rig_dir.resolve(), service, token
            )
        )
        os.environ["TUBER_RUNTIME"] = str(work)
        config = json.loads((ROOT / "config/studio.example.json").read_text(encoding="utf-8"))
        config["omni"]["base_url"] = gateway + "/v1"
        path = work / "studio.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        os.environ["TUBER_CONFIG"] = str(path)
        (work / "private-call-avatars.json").write_text(
            json.dumps(
                {
                    "origin": gateway,
                    "character_id": "whale",
                    "profiles": {
                        profile: {"accepted": True, "driver": "inochi2d", "avatar_id": name}
                        for profile, name in (("original", "regular"), ("q", "q"))
                    },
                }
            )
        )
        settings = Settings(runtime=work)
        settings.provider_keys = {"omni": token}
        app = create_app(settings)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            server = uvicorn.Server(
                uvicorn.Config(
                    app, access_log=False, log_level="error", lifespan="on", timeout_graceful_shutdown=3
                )
            )
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            serving.result()
                        await asyncio.sleep(0.05)
                playwright = await stack.enter_async_context(async_playwright())
                executable = os.getenv("CHROMIUM_EXECUTABLE")
                if not executable and os.name == "nt":
                    binaries = (Path(os.environ["LOCALAPPDATA"]) / "ms-playwright").glob(
                        "chromium_headless_shell-*/*/chrome-headless-shell.exe"
                    )
                    executable = str(max(binaries, key=lambda p: p.stat().st_mtime))
                child_env = {k: v for k, v in os.environ.items() if not k.startswith(("OMNI_", "TUBER_"))}
                microphone = work / "microphone.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-i",
                        str(args.wav),
                        "-af",
                        "adelay=5000:all=1,apad=pad_dur=45",
                        "-ar",
                        "44100",
                        "-ac",
                        "1",
                        str(microphone),
                    ],
                    check=True,
                )
                browser = await playwright.chromium.launch(
                    executable_path=executable,
                    env=child_env,
                    args=[
                        "--use-fake-device-for-media-stream",
                        "--use-fake-ui-for-media-stream",
                        f"--use-file-for-fake-audio-capture={microphone}",
                        "--autoplay-policy=no-user-gesture-required",
                        "--disable-background-networking",
                        "--no-proxy-server",
                    ],
                )
                try:
                    reference = envelope(pcm8(args.wav))
                    for profile in args.profiles:
                        result = await exercise(
                            browser, origin, profile, work, gateway, token, reference, media_python
                        )
                        result["platform_writes"] = app.state.store.one(
                            "SELECT count(*) AS n FROM account_writes"
                        )["n"]
                        print(json.dumps(result), flush=True)
                        if not result["passed"] or result["platform_writes"]:
                            raise RuntimeError("Voice-to-rig acceptance criteria not met")
                finally:
                    await browser.close()
            finally:
                server.should_exit = True
                await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-repo", type=Path, default=ROOT.parent / "omni-avatar-api")
    parser.add_argument(
        "--media-python", type=Path, help="Service interpreter for a separate source checkout"
    )
    parser.add_argument("--rig-dir", type=Path, default=ROOT / "runtime/releases/dafeiyu-avatar/master-rigs")
    parser.add_argument("--wav", type=Path, default=ROOT / "assets/whale/sample1.wav")
    parser.add_argument("--profiles", nargs="+", choices=["original", "q"], default=["original", "q"])
    args = parser.parse_args()
    service = connection(os.environ)
    if not service:
        parser.error("Set OMNI_GATEWAY_URL and an ordinary Studio credential.")
    for var in ("OMNI_STUDIO_TOKEN", "OMNI_CORE_TOKEN_FILE", "OMNI_GATEWAY_URL"):
        os.environ.pop(var, None)
    (ROOT / "tmp").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="real-private-rig-") as scratch:
        asyncio.run(run(args, Path(scratch), service))
