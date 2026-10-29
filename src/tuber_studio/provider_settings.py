"""Apply an authenticated provider change without exposing keys or dropping history."""

import asyncio
import copy

from .api_providers import VisionService, create_llm
from .config import atomic_bytes, validate_api_key
from .providers import VoiceService

FIELDS = {
    "llm": {"kind", "base_url", "model", "context_tokens", "native_context_tokens"},
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


async def _restore_broker(client, snapshot, *, context_attempted, was_local):
    resident = was_local and snapshot["resident"]
    expected = {**snapshot, "resident": resident}
    try:
        if await _broker_snapshot(client, snapshot["window"]) == expected:
            # A confirmed rejection or the broker's own rollback may already
            # have left the exact original resources. Do not reload them.
            return True
    except BaseException:
        pass
    if context_attempted or resident:
        try:
            response = await client.post("/context", json={"context_tokens": snapshot["window"]})
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
        data.setdefault(name, {}).update(patch)
        before, after = settings.data.get(name, {}), data[name]
        if (
            after.get("kind") == "openai_compatible"
            and after.get("base_url", "").rstrip("/") != before.get("base_url", "").rstrip("/")
            and settings.provider_keys.get(name)
            and not credentials.get(name)
            and name not in clear
        ):
            raise ValueError("更换API地址时须重新填写或明确清除对应Key，避免旧凭证发送到新服务")
    broker = data.setdefault("inference_broker", {})
    previous_llm = settings.data["llm"]
    broker.setdefault(
        "model", previous_llm["model"] if previous_llm["kind"] == "llama_cpp" else "MiniCPM5-2B"
    )
    broker.setdefault(
        "context_tokens", previous_llm["context_tokens"] if previous_llm["kind"] == "llama_cpp" else 131072
    )
    if data["llm"]["kind"] == "llama_cpp":
        broker.update(model=data["llm"]["model"], context_tokens=data["llm"]["context_tokens"])
        current_local_model = (
            previous_llm["model"]
            if previous_llm["kind"] == "llama_cpp"
            else settings.data.get("inference_broker", {}).get("model", "MiniCPM5-2B")
        )
        if broker.get("enabled") and data["llm"]["model"] != current_local_model:
            raise ValueError("此设置不切换本地模型；请保持已安装的默认模型，或选择外部API")
    settings._validate(data)
    return data, keys


async def apply_settings(runtime, body):
    settings = runtime.settings
    if runtime.state != "stopped" or runtime.generation_lock.locked() or runtime.mode_lock.locked():
        raise ProviderConfigurationError("请先停止直播并等待当前生成完成，再修改Provider")
    policy = runtime.store.get("comment_policy", {})
    if policy.get("auto_browse") or policy.get("auto_reply"):
        raise ProviderConfigurationError("请先关闭自动浏览和自动回复，再修改Provider")
    data, keys = proposal(settings, body)
    staged = copy.copy(settings)
    staged.data, staged.provider_keys = data, keys
    created = []
    try:
        llm = create_llm(staged)
        created.append(llm)
        voices = VoiceService(staged)
        created.append(voices)
        vision = VisionService(data["vision"], keys.get("vision"))
        created.append(vision)
    except BaseException:
        await _settle_cleanup(_close_clients(created))
        raise
    previous_data = settings.data
    was_local = previous_data["llm"]["kind"] == "llama_cpp"
    old_window = (
        previous_data["llm"]["context_tokens"]
        if was_local
        else previous_data.get("inference_broker", {}).get("context_tokens", 131072)
    )
    previous_file = previous_private = snapshot = None
    broker_attempted = context_attempted = files_attempted = False
    async with runtime.mode_lock, runtime.generation_lock:
        try:
            previous_file = settings.path.read_bytes() if settings.path.exists() else None
            previous_private = (
                settings.credentials_path.read_bytes() if settings.credentials_path.exists() else None
            )
            if runtime.inference_client:
                snapshot = await _broker_snapshot(runtime.inference_client, old_window)
                # A lost response can follow a committed RPC. Record ownership
                # before sending, never infer "not executed" from an exception.
                broker_attempted = True
                if data["llm"]["kind"] == "llama_cpp":
                    context_attempted = True
                    response = await runtime.inference_client.post(
                        "/context", json={"context_tokens": data["llm"]["context_tokens"]}
                    )
                    response.raise_for_status()
                else:
                    response = await runtime.inference_client.post("/idle", json={})
                    response.raise_for_status()
            if data["llm"]["kind"] == "llama_cpp":
                await llm.ensure_capacity()
            files_attempted = True
            settings.save_keys(keys)
            settings.save_public(data)
        except BaseException as failure:

            async def rollback():
                confirmed = True
                if broker_attempted:
                    confirmed = await _restore_broker(
                        runtime.inference_client,
                        snapshot,
                        context_attempted=context_attempted,
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
        old_clients = runtime.llm, runtime.voices, runtime.vision
        runtime.llm, runtime.voices, runtime.vision = llm, voices, vision
        runtime.context.provider = llm
        runtime.error = None
        await asyncio.gather(*(client.close() for client in old_clients), return_exceptions=True)
    return settings.public_data() | {"restart_required": False, "context_recount_required": True}
