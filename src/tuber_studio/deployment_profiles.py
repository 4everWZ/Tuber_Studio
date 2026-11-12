"""Business-side work-mode configurations for a paired inference node."""

from copy import deepcopy

from .http_api import endpoint
from .inference_profiles import (
    DAFEIYU_9B_CONTEXT,
    DAFEIYU_9B_DIGESTS,
    DAFEIYU_9B_MODEL,
    DAFEIYU_MODEL,
    GEMMA_ARTIFACTS,
    GEMMA_ENGINE_SHA256,
    GROUNDED_DAFEIYU_ARTIFACTS,
    GROUNDED_DAFEIYU_MODEL,
)


def select_local_voice(tts, broker):
    if broker.get("enabled") and broker.get("deployment") == "remote":
        tts.update(base_url=broker["base_url"].rstrip("/") + "/tts", local_backend="genie_onnx_int8")
    elif tts["kind"] != "gpt_sovits":
        tts.update(base_url="http://127.0.0.1:9012/tts", local_backend="compact")
    tts["kind"] = "gpt_sovits"


def business_configuration(
    current,
    node_url,
    *,
    mode="text",
    operations_model=DAFEIYU_9B_MODEL,
    live_model=DAFEIYU_9B_MODEL,
    context_tokens=None,
    native_context_tokens=None,
):
    if mode not in {"text", "live"}:
        raise ValueError("工作模式应为text或live")
    base = endpoint(node_url)
    data = deepcopy(current)
    if native_context_tokens is None:
        native_context_tokens = DAFEIYU_9B_CONTEXT if operations_model == DAFEIYU_9B_MODEL else 131072
    if context_tokens is None:
        context_tokens = native_context_tokens
    common = deepcopy(data["llm"])
    common.update(
        kind="llama_cpp",
        context_tokens=context_tokens,
        native_context_tokens=native_context_tokens,
        enable_thinking=True,
        operations_max_tokens=8192,
        prefill_timeout_seconds=1200,
        timeout_seconds=600,
        auto_compact_token_limit=native_context_tokens * 9 // 10,
        effective_context_window_percent=95,
    )
    operations = {
        **common,
        "base_url": base + "/llm",
        "model": operations_model,
        "temperature": 0.2,
        "summary_temperature": 0.2,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
    }
    live_native = DAFEIYU_9B_CONTEXT if live_model == DAFEIYU_9B_MODEL else 131072
    live = {
        **common,
        "base_url": base + "/llm",
        "model": live_model,
        "native_context_tokens": live_native,
        "context_tokens": min(context_tokens, live_native),
        "auto_compact_token_limit": live_native * 9 // 10,
        "max_tokens": 1024,
        "temperature": 1.0,
        "summary_temperature": 1.0,
    }
    # The new operations profile chooses its own thinking settings.
    operations.pop("reasoning_budget_tokens", None)
    disabled_vision = {"kind": "disabled", "location": "local", "base_url": "", "model": ""}
    vision, video_vision = _vision_profiles(base, operations)
    data["work_mode_profiles"] = {
        "text": {"llm": operations, "vision": vision},
        "live": {"llm": live, "vision": disabled_vision},
    }
    data["video_vision"] = video_vision
    if operations_model in {DAFEIYU_9B_MODEL, DAFEIYU_MODEL, GROUNDED_DAFEIYU_MODEL}:
        operations["temperature"] = operations["summary_temperature"] = 0.7
    if operations_model == GROUNDED_DAFEIYU_MODEL:
        operations["enable_thinking"] = False
    data.setdefault("features", {})["live_enabled"] = True
    data.setdefault("inference_broker", {}).update(
        enabled=True,
        deployment="remote",
        base_url=base,
        mode=mode,
        model=operations_model,
        context_tokens=context_tokens,
        kv_cache_type="q4_0",
    )
    if data["tts"]["kind"] != "openai_compatible":
        select_local_voice(data["tts"], data["inference_broker"])
    data.update(deepcopy(data["work_mode_profiles"][mode]))
    return data


def _vision_profiles(base, operations):
    model = operations["model"]
    if model == DAFEIYU_MODEL:
        return {"kind": "disabled", "location": "local", "base_url": "", "model": ""}, {"enabled": False}

    context_tokens = operations["context_tokens"]
    native_context_tokens = operations["native_context_tokens"]
    image_profile, image_order = "server_native", "text_first"
    thinking = True
    if model == DAFEIYU_9B_MODEL:
        route = "dafeiyu9b"
        model_revision = DAFEIYU_9B_DIGESTS["model"]
        projector_revision = DAFEIYU_9B_DIGESTS["projector"]
        runtime_revision = ""
        runtime_profile = "dafeiyu-qwen35-9b-q4km-q8-projector"
    elif model == GROUNDED_DAFEIYU_MODEL:
        route = "dafeiyu"
        model_revision = GROUNDED_DAFEIYU_ARTIFACTS["model"]
        projector_revision = GROUNDED_DAFEIYU_ARTIFACTS["projector"]
        runtime_revision = GROUNDED_DAFEIYU_ARTIFACTS["engine"]
        runtime_profile = "dafeiyu-dora-q4km-4x-f16"
        thinking = False
    else:
        # Existing explicitly selected profiles use the separate Gemma vision resource.
        route, model = "gemma4e2b", "Gemma4-E2B"
        model_revision = GEMMA_ARTIFACTS["model"]["sha256"]
        projector_revision = GEMMA_ARTIFACTS["vision_only_projector"]["sha256"]
        runtime_revision = GEMMA_ENGINE_SHA256
        runtime_profile = "gemma-e2b-q4-kv-q8-visiononly"
        context_tokens = native_context_tokens = 131072
        image_profile, image_order = "gemma4_image_280", "image_first"

    sampling = {"temperature": 1.0, "top_p": 0.95, "top_k": 40, "min_p": 0.0, "repeat_penalty": 1.0}
    shared = {
        "model": model, "base_url": f"{base}/vlm/{route}/v1", "enable_thinking": thinking,
        "timeout_seconds": 600, "image_profile": image_profile, "image_order": image_order,
    }
    return (
        shared | {"kind": "openai_compatible", "location": "local", "max_tokens": 8192} | sampling,
        shared | {
            "enabled": True,
            "model_revision": model_revision,
            "projector_revision": projector_revision,
            "runtime_revision": runtime_revision,
            "runtime_profile": runtime_profile,
            "context_tokens": context_tokens,
            "native_context_tokens": native_context_tokens,
            "max_frames": 3,
            "max_pixels": 1048576,
            "max_bytes": 1048576,
            "max_output_tokens": 8192,
            "sampling": sampling,
            "properties_path": f"/vlm/{route}/props",
        },
    )


def mode_configuration(data, mode):
    """Replace provider settings together; persistent conversations stay untouched."""
    profiles = data.get("work_mode_profiles")
    if not profiles:
        return deepcopy(data)
    if mode not in {"text", "live"} or set(profiles) != {"text", "live"}:
        raise ValueError("工作模式配置不完整")
    result = deepcopy(data)
    profile = profiles[mode]
    if set(profile) != {"llm", "vision"}:
        raise ValueError("工作模式仅接受语言和视觉Provider配置")
    result.update(deepcopy(profile))
    result["inference_broker"]["mode"] = mode
    return result
