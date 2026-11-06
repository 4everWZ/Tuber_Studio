"""Business-side work-mode configurations for a paired inference node."""

from copy import deepcopy

from .http_api import endpoint
from .inference_profiles import (
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
    operations_model="Gemma4-E2B",
    context_tokens=131072,
    native_context_tokens=131072,
):
    if mode not in {"text", "live"}:
        raise ValueError("工作模式应为text或live")
    base = endpoint(node_url)
    data = deepcopy(current)
    common = deepcopy(data["llm"])
    common.update(
        kind="llama_cpp",
        context_tokens=context_tokens,
        native_context_tokens=131072,
        enable_thinking=True,
        operations_max_tokens=8192,
        prefill_timeout_seconds=1200,
        timeout_seconds=600,
        auto_compact_token_limit=117964,
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
    live = {
        **common,
        "base_url": base + "/llm",
        "model": "MiniCPM5-2B",
        "max_tokens": 1024,
        "temperature": 1.0,
        "summary_temperature": 1.0,
    }
    # A previous model's reasoning cap must not carry into the chosen Gemma profile.
    operations.pop("reasoning_budget_tokens", None)
    vision = {
        "kind": "openai_compatible",
        "location": "local",
        "base_url": base + "/vlm/gemma4e2b/v1",
        "model": "Gemma4-E2B",
        "timeout_seconds": 600,
        "max_tokens": 8192,
        "enable_thinking": True,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
        "image_profile": "gemma4_image_280",
        "image_order": "image_first",
    }
    data["work_mode_profiles"] = {
        "text": {"llm": operations, "vision": vision},
        "live": {
            "llm": live,
            "vision": {"kind": "disabled", "location": "local", "base_url": "", "model": ""},
        },
    }
    data["video_vision"] = {
        "enabled": True,
        "base_url": base + "/vlm/gemma4e2b/v1",
        "model": "Gemma4-E2B",
        "model_revision": GEMMA_ARTIFACTS["model"]["sha256"],
        "projector_revision": GEMMA_ARTIFACTS["vision_only_projector"]["sha256"],
        "runtime_revision": GEMMA_ENGINE_SHA256,
        "runtime_profile": "gemma-e2b-q4-kv-q8-visiononly",
        "context_tokens": 131072,
        "native_context_tokens": 131072,
        "max_frames": 3,
        "max_pixels": 1048576,
        "max_bytes": 1048576,
        "max_output_tokens": 8192,
        "enable_thinking": True,
        "timeout_seconds": 600,
        "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 40, "min_p": 0.0, "repeat_penalty": 1.0},
        "properties_path": "/vlm/gemma4e2b/props",
        "image_profile": "gemma4_image_280",
        "image_order": "image_first",
    }
    if operations_model == DAFEIYU_MODEL:
        operations["native_context_tokens"] = native_context_tokens
        operations["temperature"] = operations["summary_temperature"] = 0.7
        data["work_mode_profiles"]["text"]["vision"] = data["work_mode_profiles"]["live"]["vision"].copy()
        data["video_vision"] = {"enabled": False}
    elif operations_model == GROUNDED_DAFEIYU_MODEL:
        operations.update(
            native_context_tokens=native_context_tokens,
            enable_thinking=False,
            temperature=0.7,
            summary_temperature=0.7,
        )
        grounded_vision = {
            "base_url": base + "/vlm/dafeiyu/v1",
            "model": operations_model,
            "enable_thinking": False,
            "image_profile": "server_native",
            "image_order": "text_first",
        }
        vision.update(grounded_vision)
        data["video_vision"].update(
            grounded_vision,
            model_revision=GROUNDED_DAFEIYU_ARTIFACTS["model"],
            projector_revision=GROUNDED_DAFEIYU_ARTIFACTS["projector"],
            runtime_revision=GROUNDED_DAFEIYU_ARTIFACTS["engine"],
            runtime_profile="dafeiyu-dora-q4km-4x-f16",
            context_tokens=context_tokens,
            native_context_tokens=native_context_tokens,
            properties_path="/vlm/dafeiyu/props",
        )
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
