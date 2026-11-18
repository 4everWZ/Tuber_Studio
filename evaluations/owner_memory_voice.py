"""Observe actual private-call user transcription events through Chromium, using a supplied speech WAV."""

import asyncio
import json
import os
import re
import subprocess
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


async def voice_probe(playwright, executable, origin, login, audio, target, expected):
    browser = await playwright.chromium.launch(executable_path=executable, args=[
        "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={audio.resolve()}",
        "--autoplay-policy=no-user-gesture-required"])
    try:
        context = await browser.new_context(service_workers="block", permissions=["microphone"])
        await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
        assert (await context.request.post(origin + "/api/auth", data={"token": login})).ok
        import base64
        wave_data = base64.b64encode(audio.read_bytes()).decode()
        await context.add_init_script("window.qaSpeech = " + json.dumps(wave_data) + """;
          navigator.mediaDevices.getUserMedia = async () => {
            const ctx = new AudioContext();
            const out = ctx.createMediaStreamDestination(), src = ctx.createBufferSource();
            const bytes = Uint8Array.from(atob(window.qaSpeech), ch=>ch.charCodeAt(0));
            src.buffer = await ctx.decodeAudioData(bytes.buffer);
            src.connect(out); await ctx.resume(); src.start(ctx.currentTime + 0.5);
            window.qaCapture = {ctx, out, src}; return out.stream;
          };""")
        page = await context.new_page()
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
        await page.goto(origin)
        await page.locator("#operator-chat-more-toggle").click()
        await page.locator("#private-call-open").click()
        await page.locator("#private-call-topic").fill(target["source_key"])
        await page.locator("#private-call-start").click()
        quote = page.locator("#private-call-utterances blockquote").first
        with wave.open(str(audio), "rb") as wav:
            end_second = math.ceil(wav.getnframes() / wav.getframerate() + 1)
        committed_manually = False
        try:
            # Submit only after an actual speech buffer has crossed the browser WS.
            for _ in range(150):
                if counts["conversation.item.input_audio_transcription.completed"]:
                    break
                if sent_seconds >= end_second:
                    await page.locator("#private-call-finish-utterance").click(timeout=3000)
                    committed_manually = True
                    break
                await asyncio.sleep(.2)
            await quote.wait_for(timeout=15000)
        except BrowserTimeout:
            return {"status":"unverified", "session_created":bool(counts["session.created"]),
                    "capabilities":capabilities, "commit_requested":committed_manually, "ui_status":await page.locator("#private-call-state").inner_text(),
                    "service_events":dict(counts),
                    "sent_pcm_seconds":sent_seconds, "committed_user_audio_items":len(commits), "final_transcript_events":len(finals)}
        transcript = await quote.inner_text()
        assert any(key in user_parts and key[0] in commits and text == transcript for key,text in finals.items())
        report = {"status":"observed", "source":"speech WAV through Chromium capture",
                  "bound_final_transcript":transcript, "committed_user_audio_items":len(commits)}
        normalize = lambda text: re.sub(r"[^\w]", "", text.casefold())
        if normalize(transcript) == normalize(expected):
            await page.get_by_label("更正本次引用的偏好").select_option(str(target["id"]))
            async def confirm(dialog):
                assert target["note"] in dialog.message and transcript in dialog.message
                await dialog.accept()
            page.once("dialog", confirm)
            async with page.expect_response(lambda r: r.url.endswith("/api/agent/memory/confirm")) as response:
                await page.get_by_role("button", name="确认用这句话更正").click()
            result = await response.value
            assert result.ok
            report["correction"] = await result.json()
            assert report["correction"] == {"id":target["id"], "revision":target["revision"]+1}
        else:
            report["correction"] = "unverified; transcript did not match the authorized sentence"
        await page.locator("#private-call-end").click()
        return report
    finally:
        await browser.close()

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
