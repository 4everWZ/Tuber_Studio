"""Observe actual private-call user transcription events through Chromium, using a supplied speech WAV."""

import json
from collections import Counter

from playwright.async_api import TimeoutError as BrowserTimeout


async def voice_probe(playwright, executable, origin, login, audio):
    browser = await playwright.chromium.launch(executable_path=executable, args=[
        "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={audio.resolve()}",
        "--autoplay-policy=no-user-gesture-required"])
    try:
        context = await browser.new_context(service_workers="block", permissions=["microphone"])
        await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/") else route.abort())
        assert (await context.request.post(origin + "/api/auth", data={"token": login})).ok
        page = await context.new_page()
        counts = Counter()
        commits, user_parts, finals = set(), set(), {}
        def frame(raw):
            event = json.loads(raw)
            kind = event.get("type", "")
            counts[kind] += 1
            if kind == "input_audio_buffer.committed":
                commits.add(event.get("item_id"))
            if kind in {"conversation.item.created", "conversation.item.added", "conversation.item.done"}:
                item = event.get("item", {})
                if item.get("type") == "message" and item.get("role") == "user":
                    user_parts.update((item.get("id"), i) for i, part in enumerate(item.get("content", []))
                                      if part.get("type") == "input_audio")
            if kind == "conversation.item.input_audio_transcription.completed":
                finals[event.get("item_id"), event.get("content_index")] = event.get("transcript", "").strip()

        page.on("websocket", lambda ws: ws.on("framereceived", frame))
        await page.goto(origin)
        await page.locator("#operator-chat-more-toggle").click()
        await page.locator("#private-call-open").click()
        await page.locator("#private-call-start").click()
        quote = page.locator("#private-call-utterances blockquote").first
        try:
            await quote.wait_for(timeout=45000)
        except BrowserTimeout:
            return {"status":"unverified", "session_created":bool(counts["session.created"]),
                    "committed_user_audio_items":len(commits), "final_transcript_events":len(finals)}
        transcript = await quote.inner_text()
        assert any(key in user_parts and key[0] in commits and text == transcript for key,text in finals.items())
        assert await page.get_by_role("button", name="确认并记住原话").count()
        await page.locator("#private-call-end").click()
        return {"status":"observed", "source":"supplied WAV through Chromium capture",
                "bound_final_transcript":transcript, "committed_user_audio_items":len(commits)}
    finally:
        await browser.close()

async def service_probe(config, directory, speech=None):
    """Use an owner-authorized sentence, without treating a synthetic speaker as proof of identity."""
    import base64
    import os
    import subprocess
    from pathlib import Path
    from urllib.parse import urlencode

    from tuber_studio.agent.private_call import mint_ticket
    from evaluations.owner_memory_device import AUTO

    # Only the Studio process can mint. No gateway credentials are inherited by the audio/WS probes.
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA") if k in os.environ}
    if speech is None:
        speech = directory / "owner-rule.wav"
        script = ("Add-Type -AssemblyName System.Speech\n"
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                  "try { $s.SetOutputToWaveFile($env:QA_WAVE); $s.Speak($env:QA_SENTENCE) } finally { $s.Dispose() }")
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env | {"QA_WAVE":str(speech), "QA_SENTENCE":AUTO}, check=True, capture_output=True, timeout=20)
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
    import asyncio
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
