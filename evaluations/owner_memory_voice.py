"""Observe actual private-call user transcription events through Chromium, using a supplied speech WAV."""

import asyncio
import json
import os
import re
import subprocess
import secrets
import wave
import math
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from tuber_studio.agent import private_call

from playwright.async_api import TimeoutError as BrowserTimeout


VOICE_RULE = "以后直播提纲的标题都用陈述句，以句号结尾。"


def tool_env():
    return {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA") if k in os.environ}


def spoken_rule(directory, sentence):
    speech = directory / "owner-rule.wav"
    script = ("Add-Type -AssemblyName System.Speech\n"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
              "if ($env:QA_SENTENCE -match '[\u3400-\u9fff]') { $s.SelectVoiceByHints(0,0,0,[cultureinfo]::GetCultureInfo('zh-CN')) }\n"
              "try { $s.SetOutputToWaveFile($env:QA_WAVE); $s.Speak($env:QA_SENTENCE) } finally { $s.Dispose() }")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   env=tool_env() | {"QA_WAVE":str(speech), "QA_SENTENCE":sentence}, check=True, capture_output=True, timeout=20)
    return speech


@asynccontextmanager
async def checkout_binding(config, checkout):
    if checkout is None:
        yield config
        return
    checkout = checkout.resolve(strict=True)
    python = checkout / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    key = secrets.token_urlsafe(32)
    server = await asyncio.create_subprocess_exec(str(python), str(Path(__file__).with_name("owner_memory_gateway.py")),
        cwd=checkout, env=tool_env() | {"PYTHONPATH":str(checkout)}, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        server.stdin.write(json.dumps([*config, key]).encode() + b"\n")
        await server.stdin.drain()
        port = int(await asyncio.wait_for(server.stdout.readline(), 10))
        yield f"http://127.0.0.1:{port}", key
    finally:
        server.stdin.close()
        try:
            await asyncio.wait_for(server.wait(), 5)
        except TimeoutError:
            server.kill()
            await server.wait()


@asynccontextmanager
async def live_voice_binding(config):
    # Substitute only the configured transport address. Studio and the live gateway still validate every request.
    root, key = config
    relay = await asyncio.create_subprocess_exec("node", str(Path(__file__).with_name("owner_memory_voice_loopback.mjs")),
        env=tool_env(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        relay.stdin.write(json.dumps({"origin":root}).encode() + b"\n")
        await relay.stdin.drain()
        port = int(await asyncio.wait_for(relay.stdout.readline(), 5))
        with patch.object(private_call, "private_service", lambda: (f"http://127.0.0.1:{port}", key)):
            yield
    finally:
        if relay.returncode is None:
            relay.terminate()
        await relay.wait()


async def voice_probe(playwright, executable, origin, login, audio, target, expected, *, correct_qa_preview=False):
    browser = await playwright.chromium.launch(executable_path=executable, args=[
        "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={audio.resolve()}",
        "--autoplay-policy=no-user-gesture-required"])
    try:
        context = await browser.new_context(service_workers="block", permissions=["microphone"])
        await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
        assert (await context.request.post(origin + "/api/auth", data={"token": login})).ok
        page = await context.new_page()
        return await voice_on_page(page, origin, audio, target, expected, correct_qa_preview=correct_qa_preview)
    finally:
        await browser.close()


async def voice_on_page(page, origin, audio, target, expected, *, correct_qa_preview=False, signed_only=False,
                        progress=None):
    """Drive the Studio call on an already authenticated page, preserving its browser and owner session."""
    import base64
    def stage(name, **facts):
        if progress:
            progress(name, facts)
    stage("audio_fixture")
    wave_data = base64.b64encode(audio.read_bytes()).decode()
    await page.goto(origin)
    await page.evaluate("""speech => {
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext();
        const out = ctx.createMediaStreamDestination(), src = ctx.createBufferSource();
        const bytes = Uint8Array.from(atob(speech), ch=>ch.charCodeAt(0));
        src.buffer = await ctx.decodeAudioData(bytes.buffer);
        src.connect(out); await ctx.resume(); window.qaStartSpeech = () => src.start(ctx.currentTime + 0.2);
        window.qaCapture = {ctx, out, src}; return out.stream;
      };}""", wave_data)
    counts = Counter()
    capabilities = {}
    sent_seconds = 0
    def sent(raw):
        nonlocal sent_seconds
        if json.loads(raw).get("type") == "input_audio_buffer.append":
            sent_seconds += 1
    commits, user_parts, finals = set(), set(), {}
    started, selected = set(), {}
    def frame(raw):
        event = json.loads(raw)
        kind = event.get("type", "")
        counts[kind] += 1
        if kind == "session.created":
            caps = event.get("session",{}).get("capabilities",{})
            capabilities.update({k:caps.get(k) for k in ("implementation_level","chunk_period_ms","supports_input_append","gateway_input_transcription")})
        if kind == "gateway.input_transcription.started":
            started.add(event["capture_id"])
        if kind == "gateway.input_transcription.completed" and event.get("source") == "explicit_user_audio":
            selected[event["capture_id"]] = event
        if kind == "input_audio_buffer.committed":
            commits.add(event.get("item_id"))
        if kind in {"conversation.item.created", "conversation.item.added", "conversation.item.done"}:
            item = event.get("item", {})
            if item.get("type") == "message" and item.get("role") == "user":
                user_parts.update((item.get("id"), i) for i, part in enumerate(item.get("content", []))
                                  if part.get("type") == "input_audio")
        if kind == "conversation.item.input_audio_transcription.completed":
            finals[event.get("item_id"), event.get("content_index")] = event.get("transcript", "").strip()

    def watch(ws):
        if (ws.url.startswith(origin.replace("https://", "wss://", 1) + "/v1/realtime?")
                if signed_only else "/v1/realtime?" in ws.url):
            ws.on("framereceived", frame)
            ws.on("framesent", sent)
    page.on("websocket", watch)
    stage("open_private_call")
    await page.locator("#operator-chat-more-toggle").click()
    await page.locator("#private-call-open").click()
    await page.get_by_text("指定通话话题", exact=True).click()
    await page.get_by_label("话题（可选）").fill(target["source_key"])
    stage("ticket_and_handshake")
    await page.locator("#private-call-start").click()
    quote = page.locator("#private-call-utterances blockquote").first
    with wave.open(str(audio), "rb") as wav:
        end_second = math.ceil(wav.getnframes() / wav.getframerate() + 1)
    committed_manually = False
    requested = False
    try:
        await page.wait_for_function("typeof window.qaStartSpeech === 'function'", timeout=30000)
        stage("signed_capture", session_created=bool(counts["session.created"]))
        if signed_only and (capabilities.get("gateway_input_transcription") or {}).get("receipt") != "hmac-sha256-v1":
            await page.locator("#private-call-end").click()
            return {"status":"unverified", "reason":"signed_transcription_not_negotiated", "service_events":dict(counts)}
        if (capabilities.get("gateway_input_transcription") or {}).get("receipt") == "hmac-sha256-v1":
            await page.get_by_role("button", name="转认下一句").click()
            await page.get_by_role("button", name="结束并转认这句").wait_for()
            requested = True
        start_second = sent_seconds
        await page.evaluate("window.qaStartSpeech()")
        stage("awaiting_final", explicit_requested=requested)
        # Submit only after an actual speech buffer has crossed the browser WS.
        for _ in range(150):
            if not requested and counts["conversation.item.input_audio_transcription.completed"]:
                break
            if sent_seconds >= start_second + end_second:
                if requested:
                    await page.get_by_role("button", name="结束并转认这句").click()
                await page.locator("#private-call-finish-utterance").click(timeout=3000)
                committed_manually = True
                break
            await asyncio.sleep(.2)
        picker = page.get_by_label("选择要处理的转写")
        options = picker.locator("option:not([value=''])")
        if requested:
            options = options.filter(has_text=re.compile("^所选录音 · "))
        await options.first.wait_for(state="attached", timeout=60000 if requested else 15000)
        await page.get_by_text("查看通话转写", exact=True).click()
        await picker.select_option(await options.first.get_attribute("value"))
        await quote.wait_for()
    except BrowserTimeout:
        result = {"status":"unverified", "session_created":bool(counts["session.created"]),
                "capabilities":capabilities, "commit_requested":committed_manually, "ui_status":await page.locator("#private-call-state").inner_text(),
                "service_events":dict(counts), "explicit_requested":requested,
                "selection_status":await page.locator("#private-call-transcribe-state").inner_text(),
                "sent_pcm_seconds":sent_seconds, "committed_user_audio_items":len(commits), "final_transcript_events":len(finals)}
        await page.locator("#private-call-end").click()
        return result
    transcript = await quote.inner_text()
    if requested:
        capture_id, = started
        wire = selected[capture_id]
        assert (0 < wire["samples"] <= 240000 and wire["transcript"] == transcript
                and re.fullmatch(r"[0-9a-f]{64}", wire.get("receipt", "")))
    else:
        assert any(key in user_parts and key[0] in commits and text == transcript for key,text in finals.items())
    report = {"status":"observed", "source":"speech WAV through Chromium capture",
              "protocol":"explicit_user_audio" if requested else "native_user_final", "service_events":dict(counts),
              "explicit_samples":wire["samples"] if requested else None, "final_transcript_events":len(finals),
              "bound_final_transcript":transcript, "committed_user_audio_items":len(commits)}
    if requested:
        report["capture_id"] = capture_id
        report["signed_final_received"] = True
    stage("final_received", **report)
    def normalize(text):
        return re.sub(r"[^\w]", "", text.casefold())
    row = quote.locator("..")
    exact = normalize(transcript) == normalize(expected)
    # This QA speaker read the authorized script. Re-enter that entire script in the visible
    # preview; keep both verbatim texts. An arbitrary supplied WAV cannot take this path.
    owner_edit = requested and correct_qa_preview and not exact
    if signed_only:
        # Allow removal of an acknowledgement from the known QA reading. Preserve unrelated ASR as failure.
        owner_edit = owner_edit and normalize(transcript) in {"好的" + normalize(expected), "好" + normalize(expected)}
    if exact or owner_edit:
        confirmed = transcript
        if owner_edit:
            confirmed = expected
            await row.get_by_label("号主确认的文字（可改正误听）").fill(confirmed)
        report["owner_confirmed_text"] = confirmed
        report["preview_edited_by_authorized_qa"] = owner_edit
        await row.get_by_label("更正本次引用的偏好").select_option(str(target["id"]))
        stage("owner_confirmation", target_id=target["id"], old_revision=target["revision"], final=transcript)
        async def confirm(dialog):
            if not all(text in dialog.message for text in (
                target["note"], transcript, confirmed, f"#{target['id']}（v{target['revision']}）",
            )):
                await dialog.dismiss()
                return
            await dialog.accept()
            report["confirmation_dialog_accepted"] = True
        page.once("dialog", confirm)
        async with page.expect_response(lambda r: r.url.endswith("/api/agent/memory/confirm")) as response:
            await row.get_by_role("button", name="确认用这句话更正").click()
        result = await response.value
        report["confirm_http_status"] = result.status
        stage("confirm_response", **report)
        assert result.ok
        report["correction"] = await result.json()
        assert report["correction"] == {"id":target["id"], "revision":target["revision"]+1}
    else:
        report["correction"] = "unverified; transcript did not match the authorized sentence"
    await page.locator("#private-call-end").click()
    return report


async def service_probe(config, directory, speech=None):
    """Use an owner-authorized sentence, without treating a synthetic speaker as proof of identity."""
    import base64
    from urllib.parse import urlencode

    from tuber_studio.agent.private_call import mint_ticket
    from evaluations.owner_memory_device import AUTO

    # Only the Studio process can mint. No gateway credentials are inherited by the audio/WS probes.
    env = tool_env()
    if speech is None:
        speech = await asyncio.to_thread(spoken_rule, directory, AUTO)
        source = "synthetic reading of the owner's AGENTS.md sentence"
    else:
        source = "authorized speech WAV"
    raw = subprocess.run(["ffmpeg","-v","error","-nostdin","-i",str(speech),"-t","12","-ac","1","-ar","16000",
                          "-f","s16le","pipe:1"], env=env, check=True, capture_output=True, timeout=20).stdout
    if not raw:
        raise ValueError("empty speech fixture")
    root, token = config
    ticket = await mint_ticket(root, token)
    url = root.replace("http","ws",1) + "/v1/realtime?" + urlencode({"ticket":ticket,"autostart":"0","duplex":"1"})
    ws = await asyncio.to_thread(subprocess.run, ["node", str(Path(__file__).with_name("owner_memory_realtime.mjs"))],
                                 input=json.dumps({"socket":url,"pcm":base64.b64encode(raw).decode()}).encode(),
                                 env=env, check=True, capture_output=True, timeout=65)
    return json.loads(ws.stdout) | {"input_source":source, "transport":"ordinary gateway ticket / native Realtime"}


if __name__ == "__main__":
    import argparse
    import asyncio
    import os
    import tempfile
    from pathlib import Path

    from evaluations.omni_chat_loop import ROOT, connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-wav", type=Path)
    args = parser.parse_args()
    service = connection(os.environ)
    if not service:
        parser.error("OMNI_GATEWAY_URL and an ordinary Studio token are required")
    with tempfile.TemporaryDirectory(dir=ROOT/"tmp", prefix="owner-voice-") as scratch:
        print(json.dumps(asyncio.run(service_probe(service, Path(scratch), args.voice_wav)), ensure_ascii=False))
