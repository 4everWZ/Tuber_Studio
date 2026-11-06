"""Select the prompt source from the model, independently of the active role card."""

from pathlib import Path

from .config import uses_broker_llm
from .inference_profiles import DAFEIYU_ALIASES, DAFEIYU_MODEL
from .inference_profiles import GROUNDED_DAFEIYU_MODEL as GROUNDED_DAFEIYU_MODEL

DAFEIYU_IDENTITY = "你是大肥鱼。"
GROUNDED_DAFEIYU_CARD = (Path(__file__).parent / "prompts/dafeiyu-grounded.md").read_text(encoding="utf-8").strip()


def uses_built_in_persona(settings):
    """Resolve the role source for the configured provider and model."""
    settings = settings or {}
    llm = settings.get("llm", {})
    if llm.get("kind") == "openai_compatible":
        return False
    model = llm.get("model", "").strip().lower()
    if model == GROUNDED_DAFEIYU_MODEL.lower():
        return llm.get("kind") == "llama_cpp"
    if model not in DAFEIYU_ALIASES:
        return False
    if model in {DAFEIYU_MODEL.lower(), "minicpm-v-4_7-1b"}:
        # The old bare alias is shared with the unmodified VLM. Only the broker owns it here.
        return uses_broker_llm(settings)
    return True


def built_in_anchor(character, settings, *, for_response=True):
    """Select the compact role for dialogue or the identity for grounded classification."""
    if not uses_built_in_persona(settings):
        return None

    grounded = settings["llm"]["model"].strip().lower() == GROUNDED_DAFEIYU_MODEL.lower()
    if grounded and not for_response:
        return DAFEIYU_IDENTITY
    bound = (character.get("fine_tuned_model") or "").strip().lower()
    expected = {GROUNDED_DAFEIYU_MODEL.lower()} if grounded else DAFEIYU_ALIASES
    if character.get("persona_type") == "fine_tuned" and bound in expected:
        anchor = (character.get("compact_system_prompt") or "").strip()
        if anchor:
            return anchor
    if grounded and character.get("id") == "whale":
        return GROUNDED_DAFEIYU_CARD
    return DAFEIYU_IDENTITY
