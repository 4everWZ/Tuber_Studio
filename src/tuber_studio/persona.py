"""Select the prompt source from the model, independently of the active role card."""

from .inference_profiles import DAFEIYU_ALIASES, DAFEIYU_MODEL


def built_in_anchor(character, settings):
    """Return an identity prompt for the built-in model, or use the role card."""
    llm = (settings or {}).get("llm", {})
    model = llm.get("model", "").strip().lower()
    if model not in DAFEIYU_ALIASES:
        return None
    if model in {DAFEIYU_MODEL.lower(), "minicpm-v-4_7-1b"}:
        # The old bare alias is shared with the unmodified VLM. Only the broker owns it here.
        broker = settings.get("inference_broker", {})
        base = broker.get("base_url", "").rstrip("/")
        if not broker.get("enabled") or llm.get("base_url", "").rstrip("/") not in {base, base + "/llm"}:
            return None

    bound = (character.get("fine_tuned_model") or "").strip().lower()
    if character.get("persona_type") == "fine_tuned" and bound in DAFEIYU_ALIASES:
        anchor = (character.get("compact_system_prompt") or "").strip()
        if anchor:
            return anchor
    return "你是大肥鱼。"
