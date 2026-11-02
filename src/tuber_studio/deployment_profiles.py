"""Business-side work-mode configurations for a paired inference node."""

from copy import deepcopy

from .http_api import endpoint
from .inference_profiles import GEMMA_ARTIFACTS, GEMMA_ENGINE_SHA256


def business_configuration(current, node_url, *, mode="text"):
    if mode not in {"text", "live"}:
        raise ValueError("工作模式应为text或live")
    base = endpoint(node_url)
    data = deepcopy(current)
    common = deepcopy(data["llm"])
    common.update(
        kind="llama_cpp",
        context_tokens=131072,
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
        "base_url": base + "/llm/gemma4e2b",
        "model": "Gemma4-E2B",
        "temperature": 0.2,
        "summary_temperature": 0.2,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
    }
    live = {
        **common,
        "base_url": base + "/llm/2b",
        "model": "MiniCPM5-2B",
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
    data.setdefault("features", {})["live_enabled"] = True
    data.setdefault("inference_broker", {}).update(
        enabled=True,
        deployment="remote",
        base_url=base,
        mode=mode,
        model="Gemma4-E2B",
        context_tokens=131072,
        kv_cache_type="q4_0",
    )
    data["tts"].update(base_url=base + "/tts", kind="gpt_sovits", local_backend="genie_onnx_int8")
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
