"""Apply an authenticated provider change without exposing keys or dropping history."""

import asyncio
import copy

import httpx

from .api_providers import VisionService, create_llm
from .config import atomic_bytes, normalize_broker_llm, validate_api_key
from .deployment_profiles import select_local_voice
from .providers import VoiceService, public_error
from .video_vision import ApiVideoEvidenceProvider

FIELDS = {
    "responses": {"base_url", "model", "max_output_tokens", "timeout_seconds"},
    "llm": {"kind", "base_url", "model", "context_tokens", "native_context_tokens", "operations_max_tokens",
            "enable_thinking", "live_thinking_tokens", "structured_output"},
    "tts": {"kind", "base_url", "model", "voice"},
    "vision": {"kind", "location", "base_url", "model"},
}


class ProviderConfigurationError(RuntimeError):
    """Fixed, non-secret operator guidance safe to expose through the settings API."""


async def _close_clients(clients):
    await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)


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


def _restore_file(path, previous, *, private=False):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_bytes(path, previous, private=private)


def _voice_only(body):
    targets = (body.keys() & FIELDS.keys()) | body.get("credentials", {}).keys()
    return targets.union(body.get("clear_credentials", [])) == {"tts"}


def proposal(settings, body):
    if not isinstance(body, dict) or set(body) - {*FIELDS, "credentials", "clear_credentials", "use_omni_video"}:
        raise ValueError("未知Provider设置字段")
    if "use_omni_video" in body and (body["use_omni_video"] is not True or "llm" not in body):
        raise ValueError("Omni 图文接入须明确指定聊天端点")
    data = copy.deepcopy(settings.data)
    keys = dict(settings.provider_keys)
    credentials, clear = body.get("credentials", {}), body.get("clear_credentials", [])
    if not isinstance(credentials, dict) or set(credentials) - set(FIELDS):
        raise ValueError("未知API凭证类型")
    if not isinstance(clear, list) or any(name not in FIELDS for name in clear):
        raise ValueError("未知凭证清除目标")
    for name, value in credentials.items():
        if value == "":
            continue
        validate_api_key(value)
        if name in clear:
            raise ValueError("同一凭证不能同时填写并清除")
        keys[name] = value
    for name in clear:
        keys.pop(name, None)
    for name, fields in FIELDS.items():
        if name == "responses" and name not in body:
            continue
        patch = body.get(name, {})
        if not isinstance(patch, dict) or set(patch) - fields:
            raise ValueError("未知Provider字段")
        before, after = settings.data.get(name, {}), data.setdefault(name, {})
        if name == "tts" and patch.get("kind") == "gpt_sovits" and "base_url" not in patch:
            select_local_voice(after, data.get("inference_broker", {}))
        after.update(patch)
        if name == "llm":
            if (
                after["kind"] != before.get("kind")
                or after["base_url"].rstrip("/") != before.get("base_url", "").rstrip("/")
                or after.get("model") != before.get("model")
            ):
                if "structured_output" not in patch:
                    after.pop("structured_output", None)
                after.pop("context_source", None)
                after.pop("prompt_token_endpoint", None)
            if "native_context_tokens" in patch and "context_tokens" not in patch:
                after["context_tokens"] = after["native_context_tokens"]
            normalize_broker_llm(data)
            video = data.get("video_vision", {})
            if "enable_thinking" in patch and video.get("enabled") and video.get("model") == after["model"] and (
                video["base_url"].rstrip("/").removesuffix("/v1") == after["base_url"].rstrip("/").removesuffix("/v1")
            ):
                video["enable_thinking"] = after["enable_thinking"]
        if (
            (
                after.get("base_url", "").rstrip("/") != before.get("base_url", "").rstrip("/")
                or (name == "tts" and after.get("kind") != before.get("kind"))
            )
            and settings.provider_keys.get(name)
            and not credentials.get(name)
            and name not in clear
        ):
            raise ValueError("更换API地址时须重新填写或明确清除对应Key，避免旧凭证发送到新服务")
    if body.get("use_omni_video"):
        model = data["llm"]
        if model["kind"] != "openai_compatible":
            raise ValueError("Omni 图文须使用兼容的 Chat 端点")
        model.update(context_source="model_card", structured_output="structural_tag", prompt_token_endpoint=True)
        old_video = data.get("video_vision") or {}
        same = old_video.get("adapter") == "omni" and all(old_video.get(key) == model[key] for key in ("base_url", "model"))
        data["video_vision"] = (old_video if same else {}) | {
            "enabled": True, "adapter": "omni", "base_url": model["base_url"], "model": model["model"],
            "enable_thinking": model.get("enable_thinking", True),
        }
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
        or runtime.generation_lock.locked()
    ):
        raise ProviderConfigurationError("请先停止直播并等待当前生成完成，再修改Provider")
    data, keys = proposal(settings, body)
    model_update = not _voice_only(body)
    if model_update:
        try:
            require_model_idle(runtime)
        except ValueError as error:
            raise ProviderConfigurationError(str(error)) from None
    staged = copy.copy(settings)
    staged.data, staged.provider_keys = data, keys
    created = []
    video = None
    try:
        if model_update:
            llm = create_llm(staged)
            created.append(llm)
        voices = VoiceService(staged)
        created.append(voices)
        voices.cache = runtime.cache
        if model_update:
            vision = VisionService(data["vision"], staged.endpoint_key("vision"))
            created.append(vision)
            if getattr(runtime, "browsing", None) and data.get("video_vision") and (
                data["video_vision"] != settings.data.get("video_vision") or
                staged.video_vision_key() != settings.video_vision_key()
            ):
                visual_config = data["video_vision"]
                video = ApiVideoEvidenceProvider(
                    runtime, visual_config, api_key=staged.video_vision_key()
                )
                created.append(video)
    except BaseException:
        await _settle_cleanup(_close_clients(created))
        raise
    previous_file = previous_private = None
    files_attempted = False
    async with runtime.generation_lock:
        try:
            if model_update:
                require_model_idle(runtime)
            previous_file = settings.path.read_bytes() if settings.path.exists() else None
            previous_private = (
                settings.credentials_path.read_bytes() if settings.credentials_path.exists() else None
            )
            if model_update and data["llm"]["kind"] == "llama_cpp":
                try:
                    await llm.ensure_capacity()
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    llm.profile_error = f"服务暂未连接: {public_error(exc)}"
                except httpx.HTTPError as exc:
                    llm_url = data.get("llm", {}).get("base_url", "")
                    if any(c in llm_url for c in ("100.", "192.168.", "10.", "172.", "192.0.2.")) or not any(
                        h in llm_url for h in ("127.0.0.1", "localhost")
                    ):
                        llm.profile_error = f"远端连接异常: {public_error(exc)}"
                    else:
                        raise
            if model_update:
                require_model_idle(runtime)
            files_attempted = True
            settings.save_keys(keys)
            settings.save_public(data)
        except BaseException as failure:

            async def rollback():
                confirmed = True
                if files_attempted:
                    # Try both independently; a public-file failure must not
                    # skip restoring the scoped credentials file.
                    for path, previous, private in (
                        (settings.path, previous_file, False),
                        (settings.credentials_path, previous_private, True),
                    ):
                        try:
                            _restore_file(path, previous, private=private)
                        except BaseException:
                            confirmed = False
                await _close_clients(created)
                return confirmed

            restored, interrupted = await _settle_cleanup(rollback())
            if not restored:
                raise ProviderConfigurationError(
                    "设置未保存；配置或本地资源恢复结果未确认，请重新检查状态"
                ) from None
            if interrupted and not isinstance(failure, asyncio.CancelledError):
                raise asyncio.CancelledError from None
            raise
        settings.data, settings.provider_keys = data, keys
        old_clients = [runtime.voices]
        runtime.voices = voices
        if model_update:
            old_clients.extend((runtime.llm, runtime.vision))
            runtime.llm, runtime.vision = llm, vision
            runtime.context.provider = llm
            runtime.error = None
            if video is not None:
                old_clients.append(runtime.browsing.replace_visual_provider(video))
        await asyncio.gather(
            *(client.close() if hasattr(client, "close") else client.aclose() for client in old_clients),
            return_exceptions=True,
        )
    return settings.public_data() | {"restart_required": False, "context_recount_required": model_update}
