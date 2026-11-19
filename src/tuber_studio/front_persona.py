"""Small, always-present character description for front-facing conversation."""

from .models import resident_character_profile


def front_identity(character):
    profile = resident_character_profile(character)
    return "\n".join(f"{label}：{profile[key]}" for key, label in (
        ("name", "角色名"), ("self_description", "身份"), ("speaking_style", "风格")
    ) if key in profile)
