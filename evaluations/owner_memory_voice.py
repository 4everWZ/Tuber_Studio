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
