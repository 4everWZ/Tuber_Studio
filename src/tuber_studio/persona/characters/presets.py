"""One-time version upgrade for unchanged, application-owned character presets."""

import hashlib
import json

from ...core.models import LEGACY_VOICE_IDS, Character

# Full normalized configs from earlier releases and the previous clean presets,
# excluding only the database-assigned version. No identity substring or
# role-ID-only matching.
_LEGACY = {
    "whale": {
        "1be94aab9f230939277bb0c46a56219b5915ddeedc05ce745f5a23c5c26db24e",
        "4bc4ed6f9159b82559fd2b69888a2989fb3c6094d1aaebaee17fc75b050a1bcd",
        "58f2eb2af9b7fb8ade8abd9449bd8c691c17fd4d495ca1d5f9b0aca8cc9ceee2",
        "7b69396338e264ddd83118b1deca9eedfa86ca3653386d4d26a44c40a30e3f0d",
        "8533eb01f26d03839652298341407eca8e49308c5171fef9e85c716603315fd2",
        "9b49a0c31bbd5a9ae82acf08a09e63cefb0aae7107b83f4f0a38cbd2f1d122be",
        "c502c1ff0d81547625a819dd59aec9f2073a3610a51c86a4b75ddebd070df598",
        "df52eb5feeab6f9f7405d6ae76915ae0f7f59ff10d500ee1c49eee22668e5097",
        "e9725899336b63ddea4eb1a4998a65cb6c0ddada9326b21dd62f6421ace2f4e1",
        "f25a6c06fce9d128c76ed3d69dd3de5c3d118e83893dcb06f8f0efe8e1b36c52",
        "fcee2e6ac6f43e51e044561b1e6033261f8a299555640bcf54dc86f85f711fb6",
        "3e16a9d1dfa4065b0c5545e0f29857bcedc84460594d8d127b77155364efacfe",
        "70c3f55e82c857052a32f0dcdf6b27292da07dfe5b24dd030fdf048c829b79f0",
        "dc3b07c1580c1cdd14c6eb3d8d92c1e272f0f21218176b5fcf5526933169a1dc",
    },
}
_REPLACEMENTS = {
    # These released seeds carry the polished tsundere / anti-canned-cliche persona upgrade.
    "whale": {
        "d4b5f856d1f7b7005a61e7b37c2acb3d035787808ccfb38daa3e93902f64e527",
        "c23d93db1d5661ac0e4147ee61d547a2796ecc2e9f01866d3582a4bbffe6984d",
        "ce86f9641554ea4020edbb975db8198976ea2f99e7d7834cd09f8a18dcb9cd64",
    },
}
# Fresh installations receive the full seed. Existing versions gain only the original upgrade fields.
_CONTENT_FIELDS = ("core", "style", "rules", "lore", "sources", "license_status")


def _fingerprint(config):
    try:
        data = Character.model_validate(config).model_dump()
    except (ValueError, TypeError):
        # Unknown fields and malformed historical/custom configs are left alone.
        return None
    data.pop("version", None)
    data.pop("aliases", None)
    data.pop("display_name", None)
    # Compare using the spelling in the original fingerprints, even after an equivalent voice rename.
    for old, current in LEGACY_VOICE_IDS.items():
        if data["voice_profile"] == current:
            data["voice_profile"] = old
            break
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
        if not known or _fingerprint(seed) not in _REPLACEMENTS[character_id]:
            continue
        # Protect the read/check/save sequence from concurrent startup workers;
        # save_character remains the owner of normal version assignment/activation.
        with store.db:
            store.db.execute("BEGIN IMMEDIATE")
            if store.one(
                "SELECT 1 FROM characters c JOIN character_archives a "
                "ON a.character_id=c.id AND a.version=c.active_version WHERE c.id=?",
                (character_id,),
            ):
                continue
            try:
                history = store.versions(character_id, include_archived=True)
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
