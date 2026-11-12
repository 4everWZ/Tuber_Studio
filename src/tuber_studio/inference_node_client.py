"""Business-owned pairing and capability discovery for replaceable inference nodes."""

import asyncio
import copy
import time
from urllib.parse import urlsplit

import httpx

from .api_providers import VisionService, create_llm
from .config import atomic_bytes, validate_api_key
from .deployment_profiles import business_configuration
from .http_api import endpoint, response_json
from .inference_profiles import (
    DAFEIYU_9B_MODEL,
    DAFEIYU_MODEL,
    GROUNDED_DAFEIYU_MODEL,
    LLM_MODELS,
    llm_resource_key,
)
from .operations.browsing.models import DisabledVideoEvidence
from .providers import VoiceService
from .runtime_modes import ObservationOwner, close_clients, inference_transition, require_model_idle
from .video_vision import ApiVideoEvidenceProvider


async def _read_capabilities(client, *, timeout):
    try:
        value = await response_json(client, "GET", "/capabilities", timeout=timeout)
    except ValueError:
        raise ValueError("推理节点协议不兼容") from None
    if value.get("protocol") != "tuber-inference-node" or value.get("protocol_version") != 1:
        raise ValueError("推理节点协议不兼容")
    return value


def _connection_error(error):
    if isinstance(error, httpx.RequestError):
        return ValueError("无法连接 Jetson 推理节点，请检查节点地址和服务状态")
    code = error.response.status_code
    if code in {401, 403}:
        return ValueError("推理节点拒绝配对凭据，请检查凭据后重试")
    if code in {404, 405} or error.response.is_redirect:
        return ValueError("推理节点协议不兼容，请检查节点地址和版本")
    return ValueError(f"推理节点请求失败（HTTP {code}）")


async def refresh_capabilities(runtime):
    owner = ObservationOwner.capture(runtime)
    if not owner.client:
        raise ValueError("尚未配置推理节点")
    try:
        value = await _read_capabilities(owner.client, timeout=15)
    except Exception as error:
        if not owner.current(runtime):
            return runtime.inference.capabilities
        if isinstance(error, (httpx.RequestError, httpx.HTTPStatusError)):
            raise _connection_error(error) from None
        raise
    if not owner.current(runtime):
        return runtime.inference.capabilities
    runtime.inference.capabilities = value | {"checked_at": time.time()}
    return runtime.inference.capabilities


def status(runtime):
    broker = runtime.settings.data.get("inference_broker", {})
    return {
        "base_url": broker.get("base_url", ""),
        "deployment": broker.get("deployment", "local"),
        "paired": bool(runtime.settings.inference_token),
        "mode": runtime.inference.status.get("mode"),
        "model": runtime.settings.data["llm"]["model"],
        "business_data": str(runtime.settings.runtime),
        "capabilities": runtime.inference.capabilities,
        "error": runtime.inference.status.get("error"),
    }


async def _settle(operation):
    task = asyncio.create_task(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def pair_node(runtime, body):
    if not isinstance(body, dict) or set(body) - {"base_url", "token"}:
        raise ValueError("仅接受推理节点地址和配对凭据")
    base = endpoint(body.get("base_url"))
    base_path = urlsplit(base).path
    if base_path == "/llm":
        base = base.removesuffix(base_path)
    settings = runtime.settings
    old_base = settings.data.get("inference_broker", {}).get("base_url", "").rstrip("/")
    same_node = base == old_base or (base_path in {"", "/llm"} and old_base == base + "/llm")
    token = body.get("token")
    token_path = settings.runtime / "inference-token"
    if token in (None, "") and same_node:
        token = settings.inference_token
        if not token:
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError):
                raise ValueError("无法读取已保存的推理节点配对凭据") from None
    if token in (None, ""):
        raise ValueError("请填写 Jetson 推理节点的配对凭据")
    try:
        validate_api_key(token)
    except ValueError:
        raise ValueError("推理节点配对凭据格式无效") from None
    require_model_idle(runtime)
    if runtime.generation_lock.locked() or runtime.inference.mode_lock.locked():
        raise ValueError("请等待当前模型任务结束")
    client = httpx.AsyncClient(
        base_url=base,
        headers={"X-Tuber-Inference-Key": token},
        timeout=httpx.Timeout(600, connect=5),
        trust_env=False,
    )
    created, adopted = [], False
    try:
        await _read_capabilities(client, timeout=10)
        async with runtime.inference.mode_lock, runtime.generation_lock:
            with inference_transition(runtime):
                require_model_idle(runtime)
                # Re-read under the business transition lock; capability lookup was read-only.
                before = await response_json(client, "GET", "/health", timeout=10)
                previous_mode = before.get("mode")
                if previous_mode not in {"text", "live"} or before.get("busy") is not False:
                    raise ValueError("推理节点正在切换或未提供可确认模式")
                paths = (token_path, settings.path)
                snapshots = [path.read_bytes() if path.exists() else None for path in paths]
                mode_attempted = files_attempted = False
                try:
                    mode_attempted = True
                    observed = await response_json(
                        client, "POST", "/mode", payload={"mode": "text"}, timeout=600
                    )
                    model = observed.get("model")
                    if observed.get("mode") != "text" or model not in {
                        "Gemma4-E2B",
                        DAFEIYU_9B_MODEL,
                        DAFEIYU_MODEL,
                        GROUNDED_DAFEIYU_MODEL,
                    }:
                        raise ValueError("节点未提供可用的运营模型，请检查 Jetson 版本及运营模型设置")
                    live_model = LLM_MODELS.get(observed.get("live_provider"),
                                               DAFEIYU_9B_MODEL if model == DAFEIYU_9B_MODEL else "MiniCPM5-2B")
                    if model in {DAFEIYU_MODEL, GROUNDED_DAFEIYU_MODEL, DAFEIYU_9B_MODEL}:
                        profile = observed["providers"][observed["selected_provider"]]
                        proposed = business_configuration(
                            settings.data,
                            base,
                            operations_model=model,
                            live_model=live_model,
                            context_tokens=profile["context_tokens"],
                            native_context_tokens=profile["native_context_tokens"],
                        )
                    else:
                        proposed = business_configuration(settings.data, base, operations_model=model, live_model=live_model)
                    settings._validate(proposed)
                    staged = copy.copy(settings)
                    staged.data, staged.inference_token = proposed, token
                    llm = create_llm(staged)
                    created.append(llm)
                    vision = VisionService(proposed["vision"], staged.endpoint_key("vision"))
                    created.append(vision)
                    voices = VoiceService(staged)
                    voices.cache = runtime.cache
                    created.append(voices)
                    visual = None
                    if getattr(runtime, "browsing", None):
                        visual = (
                            ApiVideoEvidenceProvider(runtime, proposed["video_vision"], api_key=token)
                            if proposed["video_vision"]["enabled"]
                            else DisabledVideoEvidence()
                        )
                        created.append(visual)
                    await llm.ensure_capacity()
                    capabilities = await response_json(client, "GET", "/capabilities", timeout=10)
                    identity = proposed["video_vision"]
                    key = llm_resource_key(model)
                    actual = capabilities.get("profiles", {}).get(key, {})
                    if identity["enabled"] and (
                        capabilities.get("mode") != "text"
                        or not actual.get("loaded")
                        or any(
                            expected and actual.get(key) != expected
                            for key, expected in {
                                "model_sha256": identity["model_revision"],
                                "projector_sha256": identity["projector_revision"],
                                "runtime_sha256": identity["runtime_revision"],
                            }.items()
                        )
                    ):
                        raise ValueError("节点实际模型、视觉或引擎身份与运营配置不一致")
                    require_model_idle(runtime)
                    files_attempted = True
                    atomic_bytes(paths[0], token.encode(), private=True)
                    settings.save_public(proposed)
                except BaseException:

                    async def rollback():
                        confirmed = True
                        if mode_attempted:
                            try:
                                restored = await response_json(
                                    client, "POST", "/mode", payload={"mode": previous_mode}, timeout=600
                                )
                                confirmed = restored.get("mode") == previous_mode
                            except Exception:
                                confirmed = False
                        if files_attempted:
                            for path, original in zip(paths, snapshots, strict=True):
                                try:
                                    if original is None:
                                        path.unlink(missing_ok=True)
                                    else:
                                        atomic_bytes(path, original, private=True)
                                except Exception:
                                    confirmed = False
                        await close_clients(created)
                        return confirmed

                    confirmed = await _settle(rollback())
                    if not confirmed:
                        runtime.error = "推理节点切换恢复结果待确认，请检查原节点与配置"
                        runtime.inference.status = runtime.inference.status | {
                            "state": "error",
                            "error": runtime.error,
                        }
                    raise
                old = [runtime.llm, runtime.vision, runtime.voices]
                if visual is not None:
                    old.append(runtime.browsing.replace_visual_provider(visual))
                if runtime.inference.client is not None:
                    old.append(runtime.inference.client)
                settings.data, settings.inference_token = proposed, token
                runtime.inference.client = client
                voices.settings = settings
                runtime.llm, runtime.vision, runtime.voices = llm, vision, voices
                runtime.context.provider, runtime.context.last_key = llm, None
                runtime.inference.status = observed | {"active": observed.get("active_provider")}
                runtime.inference.capabilities = capabilities | {"checked_at": time.time()}
                runtime.error = None
                runtime.inference.profile_alignment_failed = False
                adopted = True
                cleanup = asyncio.create_task(close_clients(old))
                runtime.inference.cleanup_tasks.add(cleanup)
                cleanup.add_done_callback(runtime.inference.cleanup_tasks.discard)
        return status(runtime)
    except (httpx.RequestError, httpx.HTTPStatusError) as error:
        raise _connection_error(error) from None
    finally:
        if not adopted:
            await _settle(close_clients((*created, client)))
