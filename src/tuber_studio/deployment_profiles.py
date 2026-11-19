"""One gateway for foreground chat, vision, speech and background Responses."""

from copy import deepcopy

from .http_api import endpoint
from .models import canonical_voice_id

RETIRED_SECTIONS = {"llm", "vision", "video_vision", "tts", "responses", "inference_broker", "work_mode_profiles"}
RESPONSE_OPTIONS = {"max_output_tokens", "parallel_reads", "timeout_seconds"}
VIDEO_OPTIONS = {"background_model_revision", "max_frames", "max_pixels", "max_bytes", "max_output_tokens",
                 "timeout_seconds", "frame_strategy", "image_order"}
GENERATION_OPTIONS = {"auto_compact_token_limit", "context_tokens", "native_context_tokens",
                      "min_p", "presence_penalty", "repeat_penalty", "seed"}


def omni_base(url):
    base = endpoint(url)
    return base if base.endswith("/v1") else base + "/v1"


def responses_profile(omni):
    return {**omni.get("responses_options", {}), "base_url": omni["base_url"].removesuffix("/v1") + "/llm/v1",
            "model": omni["responses_model"]}


def operation_profile(omni):
    """Operational decisions share the background text engine and gateway credential."""
    profile = responses_profile(omni)
    return omni | {
        "base_url": profile["base_url"],
        "model": profile["model"],
        "structured_output": "json_schema",
        "enable_thinking": True,
    }


def video_profile(omni):
    background = operation_profile(omni)
    options = {key: value for key, value in omni.get("video_options", {}).items() if key in VIDEO_OPTIONS}
    pin = options.pop("background_model_revision", "")
    return {**options, "base_url": background["base_url"], "model": background["model"],
            "model_revision": pin, "enable_thinking": background["enable_thinking"]}


def load_profile(saved, defaults):
    """Read old volumes in memory. Carry routes and secrets only from an identified Omni connection."""
    data = {name: value for name, value in (defaults | saved).items() if name not in RETIRED_SECTIONS}
    profile = deepcopy(defaults["omni"])
    old = saved.get("llm", {})
    video = saved.get("video_vision", {})
    speech = saved.get("tts", {})
    legacy_base = None
    if "omni" in saved:
        if not isinstance(saved["omni"], dict):
            raise ValueError("Omni 配置须为对象")
        profile.update(saved["omni"])
        if isinstance(profile.get("video_options"), dict):
            # A saved Omni checkpoint pin belongs to the former vision route.
            profile["video_options"] = {key: value for key, value in profile["video_options"].items()
                                        if key != "model_revision"}
    elif old.get("kind") == "openai_compatible" and (
        old.get("context_source") == "model_card" and old.get("model") == "omni-default"
        or video.get("adapter") == "omni" and video.get("base_url") == old.get("base_url")
        or speech.get("kind") == "omni_reference" and speech.get("base_url") == old.get("base_url")
    ):
        profile.update({name: old[name] for name in (profile.keys() | GENERATION_OPTIONS) & old.keys()
                        if name not in {"context_source", "prompt_token_endpoint", "structured_output"}})
        legacy_base = endpoint(old["base_url"])
        if video.get("base_url") == old["base_url"] and video.get("model") == old["model"]:
            profile["video_options"] = {key: video[key] for key in VIDEO_OPTIONS & video.keys()}
        responses = saved.get("responses", {})
        if responses.get("base_url", "").rstrip("/") == responses_profile(profile)["base_url"]:
            profile["responses_model"] = responses["model"]
            profile["responses_options"].update({key: responses[key] for key in RESPONSE_OPTIONS & responses.keys()})
    profile["base_url"] = omni_base(profile["base_url"])
    data["omni"] = profile
    selected = saved.get("voice_id") or speech.get("voice_id")
    if not selected:
        legacy_voice = canonical_voice_id(speech.get("voice"))
        if speech.get("kind") == "openai_compatible":
            selected = "external_api"
        elif speech.get("kind") == "prerecorded":
            selected = "whale_sample_v1"
        elif legacy_voice in ("whale_reference", "yaoyao_reference", "whale_sample_v1", "external_api"):
            selected = legacy_voice
    data["voice_id"] = canonical_voice_id(selected or defaults["voice_id"])
    return data, legacy_base
