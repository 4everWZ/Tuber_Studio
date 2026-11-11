"""The current platform account and OC identity shared by model tasks."""

import hashlib
import json
import re
from copy import deepcopy

_START = "<current-account-identity>"
_END = "</current-account-identity>"
_BLOCK = re.compile(r"\n?" + re.escape(_START) + r"\n(.*?)\n" + re.escape(_END), re.DOTALL)


def identity_snapshot(account_actor, character):
    """Version identity facts, excluding profile-check times and credentials."""
    value = {
        "platform": account_actor.get("platform", "bilibili"),
        "account_uid": account_actor.get("account_uid"),
        "account_display_name": account_actor.get("account_display_name"),
        "account_bio": account_actor.get("account_bio"),
        "character_id": character["id"],
        "character_name": character["name"],
        "character_version": character["version"],
    }
    identity = value | {"edit": character["_edit_identity"]} if "_edit_identity" in character else value
    value["identity_version"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def runtime_identity(runtime, character, account_id=None):
    resolve = getattr(runtime, "identity_snapshot", None)
    return resolve(character, account_id) if resolve else identity_snapshot({"account_uid": account_id}, character)


def captured_identity(messages):
    """Read only our system block; external dialogue cannot supply identity."""
    for message in reversed(messages):
        if message.get("role") != "system" or not isinstance(message.get("content"), str):
            continue
        blocks = _BLOCK.findall(message["content"])
        if blocks:
            try:
                value = json.loads(blocks[-1])
            except (TypeError, ValueError):
                return None
            if isinstance(value, dict) and value.get("identity_version"):
                return value
    return None


def identity_messages(messages, identity):
    """Replace the identity block without changing persona or dialogue roles."""
    result = deepcopy(messages)
    system = None
    for message in result:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] = _BLOCK.sub("", message["content"])
            system = message
    if system is None:
        system = {"role": "system", "content": ""}
        result.insert(0, system)
    system["content"] += (
        "\n" + _START + "\n"
        + json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        + "\n" + _END
    )
    return result


def provider_identity_messages(provider, messages):
    """Use a task's captured identity, or the provider's current runtime identity."""
    if captured_identity(messages) is not None:
        return messages
    resolve = getattr(provider, "identity_resolver", None)
    return identity_messages(messages, resolve()) if resolve is not None else messages
