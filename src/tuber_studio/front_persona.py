"""Small, always-present character description for front-facing conversation."""

import re


def _first_clause(value):
    if not isinstance(value, str):
        return ""
    clause = re.split(r"[，,。！？!?；;\n]|\.\s", value.strip(), maxsplit=1)[0].strip()
    return clause if len(clause) <= 80 else clause[:79].rstrip() + "…"


def front_identity(character):
    identity = character.get("self_description") or _first_clause(character.get("core", {}).get("identity"))
    style = character.get("speaking_style") or _first_clause(character.get("style", {}).get("instructions"))
    return "\n".join(text for text in (
        f"角色名：{character['name']}", f"身份：{identity}" if identity else "",
        f"风格：{style}" if style else "",
    ) if text)
