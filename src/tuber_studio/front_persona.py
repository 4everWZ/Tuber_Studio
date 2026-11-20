"""Character-card projections shared by conversation and delivery."""

import json
import re

from .models import resident_character_profile


def character_names(character):
    names = (character.get("name"), *(character.get("aliases") or ()))
    return tuple(dict.fromkeys(name for name in names if name))


def without_address(text, character):
    names = "|".join(re.escape(name) for name in sorted(character_names(character), key=len, reverse=True))
    return re.sub(rf"^(?:{names})[ ，,、：:]*", "", text, count=1, flags=re.I) if names else text


def front_identity(character):
    profile = resident_character_profile(character)
    if "aliases" in profile:
        profile["aliases"] = "、".join(profile["aliases"])
    return "\n".join(f"{label}：{profile[key]}" for key, label in (
        ("name", "角色名"), ("aliases", "常用称呼"), ("self_description", "身份"), ("speaking_style", "风格")
    ) if key in profile)


def expressive_persona(character):
    return resident_character_profile(character) | {
        "core": character["core"], "style": character["style"], "rules": character["rules"],
    }


def persona_instructions(character):
    """The complete expressive card; account bindings and Web presentation are separate."""
    profile = expressive_persona(character) | {"lore": character.get("lore", [])}
    core = profile["core"]
    if isinstance(core.get("experiences"), list):
        profile["core"] = core | {"experiences": [
            {key: entry[key] for key in ("title", "text") if key in entry} if isinstance(entry, dict) else entry
            for entry in core["experiences"]
        ]}
    return "当前角色卡（背景、性格、表达和示例）：\n" + json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
