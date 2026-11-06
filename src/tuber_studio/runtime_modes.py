"""Stage business clients after node residency is confirmed, then commit together."""

import asyncio
import copy
from contextlib import contextmanager
from dataclasses import dataclass

from .api_providers import VisionService, create_llm
from .deployment_profiles import mode_configuration

PROFILE_ALIGNMENT_ERROR = "节点模式与业务Provider未对齐，请重新选择目标模式"


def profile_alignment_error(runtime, observed):
    profiles = runtime.settings.data.get("work_mode_profiles")
    if not profiles:
        return None
    profile = profiles.get(observed.get("mode"), {})
    if runtime.inference.profile_alignment_failed or any(
        runtime.settings.data.get(key) != profile.get(key) for key in ("llm", "vision")
    ):
        return PROFILE_ALIGNMENT_ERROR
    llm = runtime.settings.data["llm"]
    # External API profiles have their own model identities; the node cannot attest them.
    if runtime.settings.broker_key_for(llm.get("base_url", "")) and observed.get("model") != llm.get("model"):
        return PROFILE_ALIGNMENT_ERROR
    return None


@dataclass(frozen=True)
class ObservationOwner:
    client: object
    configuration: object
    revision: int

    @classmethod
    def capture(cls, runtime):
        return cls(runtime.inference.client, runtime.settings.data, runtime.inference.revision)

    def current(self, runtime):
        return (
            runtime.inference.client is self.client
            and runtime.settings.data is self.configuration
            and runtime.inference.revision == self.revision
        )


@contextmanager
def inference_transition(runtime):
    """Invalidate observations from before and during a mode or node transition."""
    runtime.inference.revision = runtime.inference.revision + 1
    try:
        yield
    finally:
        runtime.inference.revision += 1


def operation_generation_context(runtime, settings, *, lane=None):
    """Separate-model deployments wait for operations; legacy shared models keep their lock."""
    return (
        runtime.offline_generation(lane)
        if lane or settings.data.get("work_mode_profiles")
        else runtime.generation_lock
    )


async def apply_mode_profile(runtime, mode):
    settings = runtime.settings
    if not settings.data.get("work_mode_profiles"):
        return
    proposed = mode_configuration(settings.data, mode)
    settings._validate(proposed)
    staged = copy.copy(settings)
    staged.data = proposed
    created = []
    try:
        llm = create_llm(staged)
        created.append(llm)
        vision = VisionService(proposed["vision"], staged.endpoint_key("vision"))
        created.append(vision)
        await llm.ensure_capacity()
        # There is no await between persistent commit and the in-memory swap.
        settings.save_public(proposed)
    except BaseException:
        cleanup = asyncio.create_task(close_clients(created))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        raise
    old = runtime.llm, runtime.vision
    settings.data = proposed
    runtime.llm, runtime.vision = llm, vision
    runtime.context.provider = llm
    runtime.context.last_key = None
    runtime.inference.profile_alignment_failed = False
    runtime.error = None
    cleanup = asyncio.create_task(close_clients(old))
    runtime.inference.cleanup_tasks.add(cleanup)
    cleanup.add_done_callback(runtime.inference.cleanup_tasks.discard)


async def close_clients(clients):
    await asyncio.gather(
        *((getattr(client, "aclose", None) or client.close)() for client in clients), return_exceptions=True
    )
