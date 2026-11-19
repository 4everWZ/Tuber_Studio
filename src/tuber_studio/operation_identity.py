"""The current platform account and OC identity shared by model tasks."""

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_START = "<current-account-identity>"
_END = "</current-account-identity>"
_BLOCK = re.compile(r"\n?" + re.escape(_START) + r"\n(.*?)\n" + re.escape(_END), re.DOTALL)
_omit_identity = ContextVar("omit_model_identity", default=False)


@contextmanager
def without_model_identity():
    """Use only supplied source material for this model call."""
    token = _omit_identity.set(True)
    try:
        yield
    finally:
        _omit_identity.reset(token)

visual_editor_context = without_model_identity


BILIBILI_OWNER_INSTRUCTION = (
    "bilibili_owner 是当前角色绑定的 B 站号主。有人问你是谁家的鱼时，用自己的口吻回答，"
    "号主昵称以这份资料为准。speaker_is_bilibili_owner 表示当前发言人的平台 UID 是否匹配。"
)


def bilibili_owner_context(character, platform, speaker_uid):
    owner = character.get("bilibili_owner")
    if not owner:
        return {}
    return {
        "bilibili_owner": {"uid": owner["uid"], "name": owner["name"]},
        "speaker_is_bilibili_owner": platform == "bilibili" and str(speaker_uid) == owner["uid"],
    }


def private_participant_instructions(character, platform=None, speaker_uid=None):
    owner = bilibili_owner_context(character, platform, speaker_uid)
    participants = {"当前交谈者": "工坊号主", "角色": character["name"],
                    "bilibili_owner": owner.get("bilibili_owner")}
    if owner and platform == "bilibili" and speaker_uid:
        participants["speaker_is_bilibili_owner"] = owner["speaker_is_bilibili_owner"]
    return (
        "角色卡中的 bilibili_owner 是这名角色的主人，空值表示尚未绑定。"
        "运营账号的昵称和简介描述的是角色使用的平台账号。\n私聊参与者："
        + json.dumps(participants, ensure_ascii=False)
    )


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
    if _omit_identity.get() or captured_identity(messages) is not None:
        return messages
    resolve = getattr(provider, "identity_resolver", None)
    return identity_messages(messages, resolve()) if resolve is not None else messages
