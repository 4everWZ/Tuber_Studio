"""Apply an authenticated provider change without exposing keys or dropping history."""

import asyncio
import copy

from ...core.config import omni_base
from ...core.validation import validate_api_key
from .api_providers import create_llm, create_operation_llm
from .providers import VoiceService
from .video_vision import ApiVideoEvidenceProvider

EDITABLE = {"base_url", "model", "responses_model", "operations_max_tokens", "max_tokens",
            "enable_thinking", "responses_options", "video_options"}


class ProviderConfigurationError(RuntimeError):
    """Fixed, non-secret operator guidance safe to expose through the settings API."""


async def _close_clients(clients):
    await asyncio.gather(*(client.close() if hasattr(client, "close") else client.aclose() for client in clients),
                         return_exceptions=True)


async def _settle_cleanup(operation):
    """Keep ownership until cleanup finishes, including repeated caller cancels."""
    task = asyncio.create_task(operation)
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    return task.result(), interrupted


def proposal(settings, body):
    if not isinstance(body, dict) or set(body) - {"omni", "credentials", "clear_credentials"}:
        raise ValueError("仅支持 Omni 设置")
    patch = body.get("omni", {})
    credentials = body.get("credentials", {})
    clear = body.get("clear_credentials", [])
    if not isinstance(patch, dict) or set(patch) - EDITABLE:
        raise ValueError("未知 Omni 设置字段")
    if not isinstance(credentials, dict) or credentials.keys() - {"omni"} or clear not in ([], ["omni"]):
        raise ValueError("仅支持 Omni 凭证")
    key = credentials.get("omni", "")
    if key != "":
        validate_api_key(key)
        if clear:
            raise ValueError("同一凭证不能同时填写并清除")
    data = copy.deepcopy(settings.data)
    previous = data["omni"]
    updated = previous | patch
    updated["base_url"] = omni_base(updated["base_url"])
    if "responses_options" in patch:
        if not isinstance(patch["responses_options"], dict):
            raise ValueError("后台参数无效")
        updated["responses_options"] = previous.get("responses_options", {}) | patch["responses_options"]
    if "video_options" in patch:
        video = patch["video_options"]
        if not isinstance(video, dict) or set(video) != {"max_frames"}:
            raise ValueError("视觉设置仅支持视频抽帧上限")
        updated["video_options"] = previous.get("video_options", {}) | video
    if (previous["base_url"], previous["responses_model"]) != (updated["base_url"], updated["responses_model"]):
        updated.get("video_options", {}).pop("background_model_revision", None)
    keys = dict(settings.provider_keys)
    if previous["base_url"] != updated["base_url"] and keys and not (key or clear):
        raise ValueError("更换 Omni 地址时须重新填写或清除旧凭证")
    if clear:
        keys.clear()
    elif key:
        keys["omni"] = key
    data["omni"] = updated
    settings._validate(data)
    return data, keys


def require_model_idle(runtime):
    """Saved channel preferences do not prevent changing an idle deployment."""
    if runtime.live.state != "stopped":
        raise ValueError("请先停止直播再修改推理配置")
    if runtime.operations.policy()["enabled"]:
        raise ValueError("请先停止自主运营再修改推理配置")
    browsing = getattr(runtime, "browsing", None)
    if browsing and (
        getattr(browsing, "active", None)
        or getattr(getattr(browsing, "comments", None), "active_task_id", None)
    ):
        raise ValueError("请等待当前运营任务结束再修改推理配置")


async def apply_settings(runtime, body):
    settings = runtime.settings
    if (
        runtime.live.state != "stopped"
        or runtime.generation_active()
    ):
        raise ProviderConfigurationError("请先停止直播并等待当前生成完成，再修改Provider")
    async with runtime.generation_lock, runtime.operation_generation_lock:
        try:
            require_model_idle(runtime)
        except ValueError as error:
            raise ProviderConfigurationError(str(error)) from None
        data, keys = proposal(settings, body)
        staged = copy.copy(settings)
        staged.data, staged.provider_keys = data, keys
        created = []
        video = None
        try:
            llm = create_llm(staged)
            created.append(llm)
            operation_llm = create_operation_llm(staged)
            created.append(operation_llm)
            voices = VoiceService(staged)
            created.append(voices)
            voices.cache = runtime.cache
            if getattr(runtime, "browsing", None) and (
                staged.video_config != settings.video_config or staged.provider_key("omni") != settings.provider_key("omni")
            ):
                video = ApiVideoEvidenceProvider(
                    runtime, staged.video_config, api_key=staged.provider_key("omni"), background=True
                )
                created.append(video)
            settings.save_connection(data, staged.provider_keys)
        except BaseException:
            await _settle_cleanup(_close_clients(created))
            raise
        old_clients = [runtime.llm, runtime.operation_llm, runtime.voices]
        settings.data, settings.provider_keys = data, staged.provider_keys
        runtime.llm, runtime.voices = llm, voices
        runtime.context.provider = llm
        runtime.schedule_connection_probe()
        runtime.operation_llm = operation_llm
        runtime.error = None
        if video is not None:
            old_clients.append(runtime.browsing.replace_visual_provider(video))
        _, interrupted = await _settle_cleanup(_close_clients(old_clients))
        if interrupted:
            raise asyncio.CancelledError
    return settings.public_data() | {"restart_required": False, "context_recount_required": True}
