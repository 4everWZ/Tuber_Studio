"""Probe synthetic input acceptance, not long-context recall or persona quality.

Run in the project environment: python tools/evaluate_context.py --config runtime/studio.json
Only small JSON events are printed; neither the filler prompt nor token arrays are saved.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import time
from collections import deque
from pathlib import Path

import httpx

ROOT = Path(os.getenv("TUBER_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def memory():
    mem = {
        line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
    }
    vm = {line.split()[0]: int(line.split()[1]) for line in Path("/proc/vmstat").read_text().splitlines()}
    return {
        "available_kib": mem["MemAvailable"],
        "swap_used_kib": mem["SwapTotal"] - mem["SwapFree"],
        "pswpin": vm["pswpin"],
        "pswpout": vm["pswpout"],
        "oom_kill": vm["oom_kill"],
    }


class ProbeCancelled(RuntimeError):
    pass


class MemoryWatch:
    def __init__(self, deadline):
        self.started = time.monotonic()
        self.deadline = deadline
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.baseline = memory()
        self.minimum_available_kib = self.baseline["available_kib"]
        self.maximum_rolling_swap_bytes = 0
        self.recent = deque([(self.started, self.baseline)])
        self.latest = self.baseline
        self.stage = "preflight"
        self.count = 0

    def sample(self):
        now, current = time.monotonic(), memory()
        self.latest = current
        self.count += 1
        self.minimum_available_kib = min(self.minimum_available_kib, current["available_kib"])
        # Retain one sample at/before the 25-second boundary, plus newer samples.
        while len(self.recent) > 1 and self.recent[1][0] <= now - 25:
            self.recent.popleft()
        past_time, past = self.recent[0]
        swapped = self.page_size * (current["pswpin"] - past["pswpin"] + current["pswpout"] - past["pswpout"])
        self.maximum_rolling_swap_bytes = max(self.maximum_rolling_swap_bytes, swapped)
        self.recent.append((now, current))
        if current["oom_kill"] > self.baseline["oom_kill"]:
            raise ProbeCancelled("System OOM counter increased")
        if current["available_kib"] < 160 * 1024:
            raise ProbeCancelled("Available memory below 160 MiB")
        if now - past_time >= 25 and swapped > 64 * 1024 * 1024:
            raise ProbeCancelled("Swap traffic above 64 MiB over the rolling 25-second interval")
        if now - self.started >= self.deadline:
            raise ProbeCancelled("Configured wall-clock deadline reached")

    async def run(self):
        next_output = self.started + 5
        while True:
            self.sample()
            now = time.monotonic()
            if now >= next_output:
                emit(
                    "progress",
                    stage=self.stage,
                    elapsed_seconds=round(now - self.started, 3),
                    memory=self.latest,
                )
                next_output = now + 5
            await asyncio.sleep(0.5)

    def summary(self):
        current = self.latest
        return {
            "baseline": self.baseline,
            "final": current,
            "minimum_available_kib": self.minimum_available_kib,
            "swapin_bytes": (current["pswpin"] - self.baseline["pswpin"]) * self.page_size,
            "swapout_bytes": (current["pswpout"] - self.baseline["pswpout"]) * self.page_size,
            "maximum_rolling_25s_swap_bytes": self.maximum_rolling_swap_bytes,
            "oom_kill_delta": current["oom_kill"] - self.baseline["oom_kill"],
            "samples": self.count,
        }


def validate_completion(status, body, requested):
    """Require server evidence of the entire prompt and all four output tokens."""
    if status != 200:
        raise ValueError(f"Completion HTTP status {status}, expected 200")
    if not isinstance(body, dict):
        raise ValueError("Completion response must be an object")
    timings = body.get("timings", {})
    if not isinstance(timings, dict):
        raise ValueError("Invalid completion timings")
    input_counts = {
        "tokens_evaluated": body.get("tokens_evaluated"),
        "timings.prompt_n": timings.get("prompt_n"),
    }
    output_counts = {
        "tokens_predicted": body.get("tokens_predicted"),
        "timings.predicted_n": timings.get("predicted_n"),
    }
    for name, counts, expected in (("input", input_counts, requested), ("output", output_counts, 4)):
        reported = [value for value in counts.values() if value is not None]
        if not reported or any(type(value) is not int or value != expected for value in reported):
            raise ValueError(f"Server {name} counts do not prove exactly {expected} tokens: {counts}")
    if body.get("truncated") is not False:
        raise ValueError("Server must explicitly report truncated=false")
    if timings.get("cache_n", 0) not in (0, None):
        raise ValueError("Server reports cached input despite cache_prompt=false")
    return {
        "input_counts": input_counts,
        "output_counts": output_counts,
        "truncated": False,
        "cache_n": timings.get("cache_n"),
        "prompt_ms": timings.get("prompt_ms"),
        "predicted_ms": timings.get("predicted_ms"),
        "generated_text": str(body.get("content", ""))[:160],
    }


async def response_json(response, operation):
    if response.status_code != 200:
        raise RuntimeError(f"{operation} returned HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"{operation} did not return a JSON object")
    return body


async def execute(client, config, requested, watch, metadata):
    llm, broker = config["llm"], config.get("inference_broker", {})
    window, native = llm["context_tokens"], llm["native_context_tokens"]
    if type(window) is not int or type(native) is not int or window < 131072 or native < window:
        raise ValueError("Configuration must declare context>=131072 and native context>=configured context")
    if requested + 4 > window:
        raise ValueError("Requested input plus four output tokens exceeds the configured window")
    response = await client.get("/props", timeout=15)
    properties = response.json() if response.status_code == 200 else {}
    metadata["warmed_up"] = False
    if broker.get("enabled") and (response.status_code == 503 or properties.get("is_sleeping")):
        watch.stage = "typed_warmup"
        await response_json(await client.post("/warmup", json={}, timeout=180), "Typed warmup")
        metadata["warmed_up"] = True
        response = await client.get("/props", timeout=15)
    properties = await response_json(response, "Read-only props")
    aliases = properties.get("model_alias")
    aliases = [aliases] if isinstance(aliases, str) else aliases
    actual = properties.get("default_generation_settings", {}).get("n_ctx")
    if llm["model"] not in (aliases or []):
        raise ValueError("Actual model alias does not match the configured model")
    if type(actual) is not int or actual < max(131072, window):
        raise ValueError("Actual server context is below the configured native-window requirement")
    metadata.update(
        model=llm["model"], configured_context=window, configured_native_context=native, actual_context=actual
    )
    if broker.get("enabled"):
        health = await response_json(
            await client.get(broker["base_url"].rstrip("/") + "/health", timeout=10), "Broker health"
        )
        size = httpx.URL(llm["base_url"]).path.rstrip("/").rsplit("/llm/", 1)[-1]
        provider = health.get("providers", {}).get("llm" + size, {})
        observed_native = provider.get("native_context_tokens")
        if type(observed_native) is not int or observed_native < window:
            raise ValueError("Broker GGUF native context does not cover the configured window")
        metadata.update(
            actual_native_context=observed_native,
            native_source="broker GGUF metadata",
            mode=health.get("mode"),
        )
    else:
        metadata.update(actual_native_context=None, native_source="configuration declaration only")

    watch.stage = "tokenize"
    nonce = secrets.token_hex(6)
    markers = {name: name + "_" + nonce for name in ("HEAD", "MIDDLE", "TAIL")}

    async def tokenize(text, special=False):
        body = await response_json(
            await client.post("/tokenize", json={"content": text, "add_special": special}, timeout=30),
            "Tokenize",
        )
        tokens = body.get("tokens")
        if (
            not isinstance(tokens, list)
            or not tokens
            or any(type(token) is not int or token < 0 for token in tokens)
        ):
            raise ValueError("Tokenizer did not return nonempty integer token IDs")
        return tokens

    head = await tokenize(markers["HEAD"] + "\n", True)
    middle = await tokenize("\n" + markers["MIDDLE"] + "\n")
    tail = await tokenize("\n" + markers["TAIL"] + "\n")
    filler = await tokenize("Synthetic capacity filler.\n")
    remaining = requested - len(head) - len(middle) - len(tail)
    if remaining < 2:
        raise ValueError("--tokens is too small for the three unique markers and filler")

    def fill(count):
        return (filler * ((count + len(filler) - 1) // len(filler)))[:count]

    left = remaining // 2
    prompt = head + fill(left) + middle + fill(remaining - left) + tail
    assert len(prompt) == requested
    metadata.update(
        markers=markers, middle_token_offset=len(head) + left, tail_token_offset=requested - len(tail)
    )
    emit("prepared", requested_tokens=requested, **metadata)
    watch.stage = "completion"
    response = await client.post(
        "/completion",
        json={
            "model": llm["model"],
            "prompt": prompt,
            "n_predict": 4,
            "ignore_eos": True,
            "cache_prompt": False,
            "stream": False,
            "temperature": 0,
        },
        timeout=None,  # The independent wall-clock monitor owns the hard deadline.
    )
    return validate_completion(response.status_code, response.json(), requested)


async def confirm_cleanup(client, broker):
    """Read-only confirmation, bounded separately from the cancelled probe deadline."""
    if not broker.get("enabled") or not broker.get("base_url"):
        return None
    started = time.monotonic()
    while time.monotonic() - started < 30:
        try:
            response = await client.get(broker["base_url"].rstrip("/") + "/health", timeout=3)
            health = response.json()
            if response.status_code == 200 and not health.get("busy", True):
                return {
                    "busy": False,
                    "state": health.get("state"),
                    "active_provider": health.get("active_provider"),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(0.5)
    return {"confirmed": False, "elapsed_seconds": round(time.monotonic() - started, 3)}


async def evaluate(config_path, requested, deadline):
    config = json.loads(config_path.read_text(encoding="utf-8"))
    broker = config.get("inference_broker", {})
    token = (ROOT / "runtime/inference-token").read_text().strip() if broker.get("enabled") else None
    watch = MemoryWatch(deadline)
    metadata, evidence, reason, cleanup = {}, None, None, None
    probe_elapsed = None
    async with httpx.AsyncClient(
        base_url=config["llm"]["base_url"].rstrip("/"),
        headers={"X-Tuber-Inference-Key": token} if token else None,
        trust_env=False,
    ) as client:
        work = asyncio.create_task(execute(client, config, requested, watch, metadata))
        monitor = asyncio.create_task(watch.run())
        try:
            done, _ = await asyncio.wait({work, monitor}, return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            evidence = await work
            watch.sample()
        except (Exception, asyncio.CancelledError) as error:
            reason = str(error) if not isinstance(error, asyncio.CancelledError) else "Caller cancelled probe"
        finally:
            work.cancel()
            monitor.cancel()
            await asyncio.gather(work, monitor, return_exceptions=True)
            probe_elapsed = round(time.monotonic() - watch.started, 3)
            if reason:
                cleanup = await confirm_cleanup(client, broker)
            # Cleanup has its own bound. Do not mislabel a final snapshot after
            # a sampling gap as another rolling 25-second measurement.
            watch.latest = memory()
            watch.minimum_available_kib = min(watch.minimum_available_kib, watch.latest["available_kib"])
    emit(
        "summary",
        accepted_requested_tokens=evidence is not None and reason is None,
        verified_120k_input=evidence is not None and reason is None and requested >= 120000,
        requested_tokens=requested,
        deadline_seconds=deadline,
        probe_elapsed_seconds=probe_elapsed,
        elapsed_seconds=round(time.monotonic() - watch.started, 3),
        reason=reason,
        profile=metadata,
        evidence=evidence,
        memory=watch.summary(),
        broker_cleanup=cleanup,
        limitation="Synthetic input acceptance only; not a test of long-text recall, factual accuracy or persona quality.",
    )
    return evidence is not None and reason is None


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "runtime/studio.json")
    parser.add_argument(
        "--tokens",
        type=positive_int,
        default=120000,
        help="Exact synthetic input size; small stub runs do not verify 120K",
    )
    parser.add_argument(
        "--deadline",
        type=positive_int,
        default=900,
        help="Hard end-to-end probe deadline in seconds, excluding bounded cleanup",
    )
    args = parser.parse_args()
    try:
        accepted = asyncio.run(evaluate(args.config, args.tokens, args.deadline))
    except KeyboardInterrupt:
        emit("summary", accepted_requested_tokens=False, verified_120k_input=False, reason="Interrupted")
        return 130
    except Exception as error:
        emit(
            "summary",
            accepted_requested_tokens=False,
            verified_120k_input=False,
            reason=type(error).__name__ + ": " + str(error),
        )
        return 1
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
