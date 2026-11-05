"""Select the prompt source from the model, independently of the active role card."""

from .config import uses_broker_llm
from .inference_profiles import DAFEIYU_ALIASES, DAFEIYU_MODEL


def uses_built_in_persona(settings):
    """Resolve the role source for the configured provider and model."""
    settings = settings or {}
    llm = settings.get("llm", {})
    if llm.get("kind") == "openai_compatible":
        return False
    model = llm.get("model", "").strip().lower()
    if model not in DAFEIYU_ALIASES:
        return False
    if model in {DAFEIYU_MODEL.lower(), "minicpm-v-4_7-1b"}:
        # The old bare alias is shared with the unmodified VLM. Only the broker owns it here.
        return uses_broker_llm(settings)
    return True


def built_in_anchor(character, settings):
    """Return the compact identity for the built-in model."""
    if not uses_built_in_persona(settings):
        return None

    bound = (character.get("fine_tuned_model") or "").strip().lower()
    if character.get("persona_type") == "fine_tuned" and bound in DAFEIYU_ALIASES:
        anchor = (character.get("compact_system_prompt") or "").strip()
        if anchor:
            return anchor
    return "你是大肥鱼。"
