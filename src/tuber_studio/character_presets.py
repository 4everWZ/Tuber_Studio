"""One-time version upgrade for unchanged, application-owned character presets."""

import hashlib
import json

from .models import Character

# Full normalized configs from e5d4fea, 5e0d206 and the clean default at f9f97a4,
# excluding only the database-assigned version. No identity substring or
# role-ID-only matching.
_LEGACY = {
    "whale": {
        "7b69396338e264ddd83118b1deca9eedfa86ca3653386d4d26a44c40a30e3f0d",
        "9b49a0c31bbd5a9ae82acf08a09e63cefb0aae7107b83f4f0a38cbd2f1d122be",
        "c502c1ff0d81547625a819dd59aec9f2073a3610a51c86a4b75ddebd070df598",
        "e9725899336b63ddea4eb1a4998a65cb6c0ddada9326b21dd62f6421ace2f4e1",
    },
    "whale_calm": {"494f4d61137e6354758c0acdb358471f4c5bada61263093a2deef94366c314d7"},
}
_REPLACEMENTS = {
    "whale": "fcee2e6ac6f43e51e044561b1e6033261f8a299555640bcf54dc86f85f711fb6",
    "whale_calm": "a59012ed87a55ee2a8bb0284dc66b0d5de29b298b0fcbcfc40e7d58991ed96d3",
}
_CONTENT_FIELDS = ("core", "style", "rules", "lore", "sources", "license_status")


def _fingerprint(config):
    try:
        data = Character.model_validate(config).model_dump()
    except (ValueError, TypeError):
        # Unknown fields and malformed historical/custom configs are left alone.
        return None
    data.pop("version")
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def upgrade_bundled_characters(store, seeds):
    """Append a normal version; never rewrite history, drafts or authorizations.

    Every stored version must be a known old preset. A custom historical version
    protects the role even if its active version was reverted to an old default.
    The new version itself then makes this upgrade idempotent and respects later
    explicit rollbacks. Operator-edited seed files are not migration targets.
    """
    for seed in seeds:
        character_id = seed["id"]
        known = _LEGACY.get(character_id)
        if not known or _fingerprint(seed) != _REPLACEMENTS[character_id]:
            continue
        # Protect the read/check/save sequence from concurrent startup workers;
        # save_character remains the owner of normal version assignment/activation.
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            try:
                history = store.versions(character_id)
            except (ValueError, TypeError):
                continue
            if not history or any(_fingerprint(item) not in known for item in history):
                continue
            active = store.character(character_id)
            if _fingerprint(active) not in known:
                continue
            target = Character.model_validate(seed).model_dump()
            updated = active | {field: target[field] for field in _CONTENT_FIELDS}
            store.save_character(updated)
