"""Apply an authenticated provider change without exposing keys or dropping history."""

import asyncio
import copy

import httpx

from .api_providers import VisionService, create_llm
from .config import atomic_bytes, normalize_broker_llm, uses_broker_llm, validate_api_key
from .deployment_profiles import select_local_voice
from .inference_profiles import DAFEIYU_9B_CONTEXT, DAFEIYU_9B_MODEL, LLM_MODELS, llm_resource_key
from .providers import VoiceService, public_error
from .runtime_modes import require_model_idle
from .video_vision import ApiVideoEvidenceProvider

FIELDS = {
    "llm": {"kind", "base_url", "model", "context_tokens", "native_context_tokens", "operations_max_tokens",
            "enable_thinking", "live_thinking_tokens"},
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


async def _broker_snapshot(client, fallback_window):
    response = await client.get("/health")
    response.raise_for_status()
    health = response.json()
    selected, providers = health.get("selected_provider"), health.get("providers", {})
    if (
        health.get("mode") != "text"
        or health.get("busy") is not False
        or not isinstance(selected, str)
        or not selected.startswith("llm")
        or not isinstance(providers, dict)
        or selected not in providers
        or any(
            not isinstance(value, dict) or type(value.get("running")) is not bool
            for value in providers.values()
        )
    ):
        raise ProviderConfigurationError("本地资源未处于可确认的空闲文本模式，请先结束当前任务或资源实验")
    running = {key for key, value in providers.items() if value["running"]}
    if not running.issubset({selected}):
        raise ProviderConfigurationError("其他本地资源正在使用，请先结束资源实验再修改Provider")
    window = health.get("requested_context_tokens", fallback_window)
    if type(window) is not int or window < 4096:
        raise ProviderConfigurationError("无法确认本地上下文窗口")
    if running and providers[selected].get("context_tokens") != window:
        raise ProviderConfigurationError("本地实际加载窗口与配置不一致，请先检查资源状态")
    return {"selected": selected, "window": window, "resident": bool(running)}


def _profile_calls(window, current_window, model=None):
    context = ("/context", {"context_tokens": window})
    if model is None:
        return (context,)
    select = ("/model", {"key": model})
    if window == current_window:
        return (select,)
    return (context, select) if window < current_window else (select, context)


async def _restore_broker(client, snapshot, *, attempted_window, model_attempted, was_local):
    resident = was_local and snapshot["resident"]
    expected = {**snapshot, "resident": resident}
    actual = None
    try:
        actual = await _broker_snapshot(client, snapshot["window"])
        if actual == expected:
            # A confirmed rejection or the broker's own rollback may already
            # have left the exact original resources. Do not reload them.
            return True
    except BaseException:
        pass
    model = (
        snapshot["selected"]
        if model_attempted and (actual is None or actual["selected"] != snapshot["selected"])
        else None
    )
    if model or attempted_window is not None or resident:
        current_window = actual["window"] if actual is not None else attempted_window or snapshot["window"]
        for path, payload in _profile_calls(snapshot["window"], current_window, model):
            try:
                response = await client.post(path, json=payload)
                response.raise_for_status()
            except BaseException:
                pass
    # Also execute this after a failed context rollback: an old external/idle
    # profile must not retain a newly loaded local worker.
    if not resident:
        try:
            response = await client.post("/idle", json={})
            response.raise_for_status()
        except BaseException:
            pass
    try:
        actual = await _broker_snapshot(client, snapshot["window"])
        # A restoration response can also be lost after commit. The verified
        # resulting window/residency decides, never RPC success alone.
        return actual == expected
    except BaseException:
        return False


def _restore_file(path, previous, *, private=False):
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_bytes(path, previous, private=private)


def _voice_only(body):
    targets = (body.keys() & FIELDS.keys()) | body.get("credentials", {}).keys()
    return targets.union(body.get("clear_credentials", [])) == {"tts"}


def proposal(settings, body):
    if not isinstance(body, dict) or set(body) - {*FIELDS, "credentials", "clear_credentials"}:
        raise ValueError("未知Provider设置字段")
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
        patch = body.get(name, {})
        if not isinstance(patch, dict) or set(patch) - fields:
            raise ValueError("未知Provider字段")
        before, after = settings.data.get(name, {}), data.setdefault(name, {})
        if name == "tts" and patch.get("kind") == "gpt_sovits" and "base_url" not in patch:
            select_local_voice(after, data.get("inference_broker", {}))
        after.update(patch)
        if name == "llm":
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
    if not _voice_only(body):
        if uses_broker_llm(data):
            target = llm_resource_key(data["llm"].get("model"))
            if target is None:
                raise ValueError("未知推理节点模型")
            data["llm"]["model"] = LLM_MODELS[target]
        _update_model_profiles(data, settings.data["llm"])
    settings._validate(data)
    return data, keys


def _update_model_profiles(data, previous_llm):
    broker = data.setdefault("inference_broker", {})
    broker.setdefault(
        "model", previous_llm["model"] if previous_llm["kind"] == "llama_cpp" else DAFEIYU_9B_MODEL
    )
    broker.setdefault(
        "context_tokens", previous_llm["context_tokens"] if previous_llm["kind"] == "llama_cpp" else DAFEIYU_9B_CONTEXT
    )
    if uses_broker_llm(data):
        broker.update(model=data["llm"]["model"], context_tokens=data["llm"]["context_tokens"])
    profiles = data.get("work_mode_profiles")
    mode = broker.get("mode")
    if profiles and mode in profiles:
        profiles[mode] = {name: copy.deepcopy(data[name]) for name in ("llm", "vision")}
        if "live_thinking_tokens" in data["llm"]:
            for profile in profiles.values():
                profile["llm"]["live_thinking_tokens"] = data["llm"]["live_thinking_tokens"]


async def apply_settings(runtime, body):
    settings = runtime.settings
    if (
        runtime.live.state != "stopped"
        or runtime.generation_lock.locked()
        or runtime.inference.mode_lock.locked()
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
            if getattr(runtime, "browsing", None) and data.get("video_vision") != settings.data.get("video_vision"):
                visual_config = data["video_vision"]
                video = ApiVideoEvidenceProvider(
                    runtime, visual_config, api_key=staged.broker_key_for(visual_config["base_url"]) or staged.provider_key("vision")
                )
                created.append(video)
    except BaseException:
        await _settle_cleanup(_close_clients(created))
        raise
    previous_data = settings.data
    was_local = uses_broker_llm(previous_data)
    is_local = uses_broker_llm(data)
    old_window = (
        previous_data["llm"]["context_tokens"]
        if was_local
        else previous_data.get("inference_broker", {}).get("context_tokens", 131072)
    )
    previous_file = previous_private = snapshot = None
    broker_attempted = model_attempted = files_attempted = False
    attempted_window = None
    async with runtime.inference.mode_lock, runtime.generation_lock:
        try:
            if model_update:
                require_model_idle(runtime)
            previous_file = settings.path.read_bytes() if settings.path.exists() else None
            previous_private = (
                settings.credentials_path.read_bytes() if settings.credentials_path.exists() else None
            )
            if model_update and runtime.inference.client and (was_local or is_local):
                try:
                    snapshot = await _broker_snapshot(runtime.inference.client, old_window)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    snapshot = None
                if snapshot is not None:
                    # A lost response can follow a committed RPC. Record ownership
                    # before sending, never infer "not executed" from an exception.
                    broker_attempted = True
                    if is_local:
                        window = data["llm"]["context_tokens"]
                        target = llm_resource_key(data["llm"]["model"]) if uses_broker_llm(data) else None
                        model = target if target != snapshot["selected"] else None
                        for path, payload in _profile_calls(window, snapshot["window"], model):
                            if path == "/model":
                                model_attempted = True
                            else:
                                attempted_window = window
                            response = await runtime.inference.client.post(path, json=payload)
                            response.raise_for_status()
                    else:
                        response = await runtime.inference.client.post("/idle", json={})
                        response.raise_for_status()
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
                if broker_attempted:
                    confirmed = await _restore_broker(
                        runtime.inference.client,
                        snapshot,
                        attempted_window=attempted_window,
                        model_attempted=model_attempted,
                        was_local=was_local,
                    )
                if broker_attempted or files_attempted:
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
            broker_conf = data.get("inference_broker", {})
            if broker_conf.get("enabled"):
                old_inference = runtime.inference.client
                runtime.inference.client = httpx.AsyncClient(
                    base_url=broker_conf.get("base_url", "http://127.0.0.1:9012"),
                    headers={"X-Tuber-Inference-Key": settings.inference_token},
                    timeout=httpx.Timeout(400, connect=5),
                    trust_env=False,
                )
                if old_inference is not None:
                    old_clients.append(old_inference)
            elif runtime.inference.client is not None:
                old_clients.append(runtime.inference.client)
                runtime.inference.client = None
        await asyncio.gather(
            *(client.close() if hasattr(client, "close") else client.aclose() for client in old_clients),
            return_exceptions=True,
        )
    return settings.public_data() | {"restart_required": False, "context_recount_required": model_update}
