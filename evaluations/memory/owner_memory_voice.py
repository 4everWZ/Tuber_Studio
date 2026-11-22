"""Observe actual private-call user transcription events through Chromium, using a supplied speech WAV."""

import asyncio
import json
import os
import re
import secrets
import subprocess
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import TimeoutError as BrowserTimeout

from tuber_studio.agent.voice import private_call

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
        with patch.object(private_call, "private_service", lambda settings: (f"http://127.0.0.1:{port}", key)):
            yield
    finally:
        if relay.returncode is None:
            relay.terminate()
        await relay.wait()


async def voice_probe(playwright, executable, origin, login, audio, target, expected):
    browser = await playwright.chromium.launch(executable_path=executable, args=[
        "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={audio.resolve()}",
        "--autoplay-policy=no-user-gesture-required"])
    try:
        context = await browser.new_context(service_workers="block", permissions=["microphone"])
        await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
        assert (await context.request.post(origin + "/api/v1/system/auth/login", data={"token": login})).ok
        page = await context.new_page()
        return await voice_on_page(page, origin, audio, target, expected)
    finally:
        await browser.close()


async def voice_on_page(page, origin, audio, target, expected, *, progress=None):
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
    def frame(raw):
        event = json.loads(raw)
        kind = event.get("type", "")
        counts[kind] += 1
        if kind == "session.created":
            caps = event.get("session",{}).get("capabilities",{})
            capabilities.update({k:caps.get(k) for k in ("implementation_level","chunk_period_ms","supports_input_append")})
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
        if "/v1/realtime?" in ws.url:
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
    try:
        await page.wait_for_function("typeof window.qaStartSpeech === 'function'", timeout=30000)
        stage("awaiting_final", session_created=bool(counts["session.created"]))
        await page.evaluate("window.qaStartSpeech()")
        picker = page.get_by_label("选择要处理的转写")
        options = picker.locator("option:not([value=''])")
        await options.first.wait_for(state="attached", timeout=30000)
        await page.get_by_text("查看通话转写", exact=True).click()
        await picker.select_option(await options.first.get_attribute("value"))
        await quote.wait_for()
    except BrowserTimeout:
        result = {"status":"unverified", "reason":"native_final_transcript_not_observed",
                "session_created":bool(counts["session.created"]), "capabilities":capabilities,
                "ui_status":await page.locator("#private-call-state").inner_text(), "service_events":dict(counts),
                "sent_pcm_seconds":sent_seconds, "committed_user_audio_items":len(commits), "final_transcript_events":len(finals)}
        await page.locator("#private-call-end").click()
        return result
    transcript = await quote.inner_text()
    assert any(key in user_parts and key[0] in commits and text == transcript for key,text in finals.items())
    report = {"status":"observed", "source":"speech WAV through Chromium capture",
              "protocol":"native_user_final", "service_events":dict(counts),
              "final_transcript_events":len(finals), "bound_final_transcript":transcript,
              "committed_user_audio_items":len(commits)}
    stage("final_received", **report)
    def normalize(text):
        return re.sub(r"[^\w]", "", text.casefold())
    row = quote.locator("..")
    if normalize(transcript) == normalize(expected):
        confirmed = transcript
        report["owner_confirmed_text"] = confirmed
        await row.get_by_label("更正本次引用的偏好").select_option(str(target["id"]))
        stage("owner_confirmation", target_id=target["id"], old_revision=target["revision"], final=transcript)
        async def confirm(dialog):
            if not all(text in dialog.message for text in (
                target["note"], transcript, confirmed, "更正这条偏好",
            )):
                await dialog.dismiss()
                return
            await dialog.accept()
            report["confirmation_dialog_accepted"] = True
        page.once("dialog", confirm)
        async with page.expect_response(lambda r: r.url.endswith("/api/v1/agent/memory/confirm")) as response:
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

    from evaluations.memory.owner_memory_device import AUTO
    from tuber_studio.agent.voice.private_call import mint_ticket

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

    from evaluations.agent.omni_chat_loop import ROOT, connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--voice-wav", type=Path)
    args = parser.parse_args()
    service = connection(os.environ)
    if not service:
        parser.error("OMNI_GATEWAY_URL and an ordinary Studio token are required")
    with tempfile.TemporaryDirectory(dir=ROOT/"tmp", prefix="owner-voice-") as scratch:
        print(json.dumps(asyncio.run(service_probe(service, Path(scratch), args.voice_wav)), ensure_ascii=False))
