"""Shared sources for automatically illustrating comments, messages and posts."""

import json

from fastapi import Depends, HTTPException
from pydantic import StrictBool

from .models import StrictModel


KEY = "illustration_policy"


class IllustrationPolicy(StrictModel):
    use_memes: StrictBool = True
    generate_images: StrictBool = False


def illustration_policy(store):
    return IllustrationPolicy.model_validate(store.get(KEY, {}))


def save_illustration_policy(store, value):
    policy = IllustrationPolicy.model_validate(value)
    store.set(KEY, policy.model_dump())
    return policy


def register_illustration_policy(app, store, require_admin):
    path = "/api/settings/illustrations"

    @app.get(path, dependencies=[Depends(require_admin)])
    async def read():
        return illustration_policy(store)

    @app.patch(path, dependencies=[Depends(require_admin)])
    async def update(value: IllustrationPolicy):
        changes = value.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "请选择要修改的配图方式")
        return save_illustration_policy(store, illustration_policy(store).model_dump() | changes)


def migrate_illustration_policy(store):
    if store.get(KEY) is not None:
        return
    keys = ("browsing_policy", "message_policy", "dynamic_policy")
    old = [store.get(key, {}) for key in keys]
    policy = IllustrationPolicy()
    if any(old):
        # Merge existing channel choices without expanding any channel's permission.
        policy = IllustrationPolicy(use_memes=all(p.get("use_memes", False) for p in old),
                                    generate_images=all(p.get("generate_images", True) for p in old))
    with store.db as db:
        db.execute("INSERT INTO kv(key,value) VALUES(?,?)", (KEY, policy.model_dump_json()))
        for key, value in zip(keys, old):
            if "use_memes" in value or "generate_images" in value:
                value.pop("use_memes", None)
                value.pop("generate_images", None)
                db.execute("UPDATE kv SET value=? WHERE key=?", (json.dumps(value, ensure_ascii=False), key))
