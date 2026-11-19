"""Collect an uninterrupted wall-clock acceptance window, including WHEP generation changes."""

import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

import httpx

from evaluations.workstation_whep import SAMPLE

INIT = Path(__file__).with_name("oc_window_capture.js")


def save(path, value):
    path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf8")


async def response_readback(client, work, number):
    challenge = "FISH" + secrets.token_hex(3).upper()
    receipt = {"start": time.time(), "verified": False}
    try:
        result = (
            (
                await client.post(
                    "/llm/v1/responses",
                    json={
                        "model": "llm-default",
                        "input": "只回复这个代号：" + challenge,
                        "store": True,
                        "max_output_tokens": 32,
                        "temperature": 0,
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                )
            )
            .raise_for_status()
            .json()
        )
        actual = (await client.get("/llm/v1/responses/" + result["id"])).raise_for_status().json()
        receipt.update(
            id=result["id"],
            verified=result["id"] == actual["id"]
            and result["status"] == actual["status"] == "completed"
            and any(
                challenge in part.get("text", "")
                for item in actual["output"]
                for part in item.get("content", [])
            ),
        )
    except (httpx.HTTPError, KeyError, ValueError) as error:
        receipt["error"] = type(error).__name__
    receipt["end"] = time.time()
    save(work / f"response-{number}.json", receipt)
    return receipt


async def measure(page, work, gateway, token, joined, state, seconds, mixed):
    await page.wait_for_function("Number.isFinite(ocWindow.status().start)", timeout=35000)
    status = await page.evaluate("ocWindow.status()")
    start = status["start"]
    end = start + seconds * 1000
    samples, levels, cuts, pending = [], [], [], []
    step = 0
    cut_targets = list(range(9, seconds - 3, 28))
    async with httpx.AsyncClient(
        base_url=gateway, headers={"Authorization": "Bearer " + token}, trust_env=False, timeout=15
    ) as client:
        while status["now"] < end:
            elapsed = (status["now"] - start) / 1000
            levels.append(status)
            if mixed and elapsed >= 15 * len(pending) and len(pending) < seconds // 15:
                pending.append(asyncio.create_task(response_readback(client, work, len(pending))))
            if elapsed >= step:
                sample = await page.evaluate(SAMPLE)
                samples.append(sample | {"segment": status["segment"]})
                step = int(elapsed) + 1
            if cut_targets and elapsed >= cut_targets[0] and status["playing"] and status["rms"] > 0.025:
                result = await page.evaluate("""() => {const s=ocWindow.status();
                    if(!s.playing || s.rms<=0.025) return null;
                    [...document.querySelectorAll('#private-call-dialog button')]
                      .find(btn=>btn.textContent.trim()==='打断回答').click(); return s}""")
                if result:
                    cuts.append(result)
                    cut_targets.pop(0)
            await asyncio.sleep(0.055)
            status = await page.evaluate("ocWindow.status()")
        before_hangup = (await client.get("/v1/avatar/sessions/" + joined)).raise_for_status().json()
        finish_utc = (await page.evaluate("performance.timeOrigin") + end) / 1000
        count = await page.evaluate("ocWindow.closeMedia()")
        frames = await page.evaluate("ocWindow.frames")
        await page.locator("#private-call-end").click()
        responses = await asyncio.gather(*pending)
        for index in range(count):
            blob = await page.evaluate("n=>ocWindow.media(n)", index)
            (work / f"receiver-{index}.webm").write_bytes(base64.b64decode(blob))
        for _ in range(25):
            final = (await client.get("/v1/avatar/sessions/" + joined)).raise_for_status().json()
            if final["state"] == "closed":
                break
            await asyncio.sleep(0.2)
    rows = [row for row in frames if start <= row[0] < end]
    for name, data in (("frames", rows), ("stats", samples), ("levels", levels), ("interrupts", cuts)):
        save(work / (name + ".json"), data)
    latest = {sample["video"]["ssrc"]: sample["video"] for sample in samples if "video" in sample}
    checks = []
    for cut in cuts:
        after = [row for row in levels if row["now"] > cut["now"]]
        new = next((row for row in after if row["segment"] > cut["segment"] and row["playing"]), None)
        silence = next((row for row in after if not row["playing"] or row["rms"] < 0.004), None)
        checks.append(
            {
                "silent_ms": silence["now"] - cut["now"] if silence else None,
                "new_peer_play_ms": new["now"] - cut["now"] if new else None,
            }
        )
    return dict(
        seconds=seconds,
        start_browser_ms=start,
        end_browser_ms=end,
        avatar_id=before_hangup["avatar_id"],
        accepted_ack_ms=before_hangup["playback"].get("played_ms"),
        native=state["native"],
        whep_201=state["whep_201"],
        source_bound=state.get("source_verified", state["bound_source"]),
        state_before_hangup=before_hangup["state"],
        closed=final["state"] == "closed",
        frames=rows[-1][2] if rows else 0,
        fps=rows[-1][2] / seconds if rows else 0,
        freezes=sum(item.get("freezes", 0) for item in latest.values()),
        interrupts=checks,
        response_readbacks=sum(r["verified"] and r["end"] <= finish_utc for r in responses),
        mixed=mixed,
        passed=False,
    )
