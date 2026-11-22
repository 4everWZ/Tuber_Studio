"""Existing, character-bound images for the private chat Pet."""

import json
from hashlib import sha256
from urllib.parse import urlsplit

from PIL import Image
from pydantic import Field

from ...core.models import StrictModel
from ..assets.memes import ASSET_ID, MemeCatalog
from .performance import Performance, performance, save_performance


def _https(value):
    try:
        parts = urlsplit(value or "")
        return parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password
    except (TypeError, ValueError):
        return False


def _attributed(item):
    source, license = item["source"], item["license"]
    return (source.get("author") and license.get("name")
            and _https(source.get("url")) and _https(license.get("url")))


class PetAppearances:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.catalog = MemeCatalog(settings, store)

    def _character(self, character_id):
        character = self.store.character()
        if character["id"] != character_id:
            raise ValueError("当前角色已切换，请重新读取宠物清单")
        return character

    def _portraits(self, character, asset_id=None):
        try:
            path = self.settings.asset_path(character["avatar_manifest"])
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (KeyError, OSError, ValueError):
            return []
        entries = manifest if isinstance(manifest, list) else manifest.get("entries", manifest.get("assets", []))
        credit = {} if isinstance(manifest, list) else manifest.get("attribution", {}).get("images", {})
        items = []
        for entry in entries:
            tag = entry.get("tag")
            if not tag or tag.startswith("sing_"):
                continue
            try:
                image = self.settings.asset_path(entry["path"])
            except (KeyError, OSError, ValueError):
                continue
            if image.suffix.lower() not in {".gif", ".webp", ".png", ".jpg", ".jpeg"}:
                continue
            relative = image.relative_to(self.settings.root).as_posix()
            identity = "portrait_" + sha256(relative.encode()).hexdigest()
            if asset_id is not None and asset_id != identity:
                continue
            try:
                with Image.open(image, formats=("GIF", "WEBP", "PNG", "JPEG")) as decoded:
                    animated = getattr(decoded, "is_animated", False)
                    decoded.load()
            except (OSError, ValueError, Image.DecompressionBombError):
                continue
            origin = entry.get("source")
            owned = entry.get("license_status") == "owned" and isinstance(origin, str) and bool(origin.strip())
            source = {"author": entry.get("author") or credit.get("author"),
                      "url": origin if _https(origin) else "", "note": "" if _https(origin) else origin}
            license = {"name": credit.get("license"), "url": credit.get("license_url")}
            if owned and not license["name"]:
                license["name"] = "角色清单登记为自有素材"
            if not license["url"] and credit.get("repository") and credit.get("revision"):
                license["url"] = f"{credit['repository']}/blob/{credit['revision']}/LICENSE"
            item = {
                "asset_id": identity,
                "title": entry.get("title") or {"idle": "默认形象", "talking": "打招呼", "confused": "困惑",
                         "gift": "收到礼物", "cheerful": "开心", "angry": "生气"}.get(tag, tag),
                "style": entry.get("style") or entry.get("artwork_version"), "emotions": entry.get("emotions") or [tag],
                "animated": animated,
                "preview_url": "/" + relative, "source": source, "license": license,
                "enabled": True, "missing": False, "is_default": tag == "idle",
                "manifest_owned": owned,
            }
            if owned or _attributed(item):
                items.append(item)
        return items

    def _whale_manifest(self, character):
        return character.get("avatar_manifest") == "assets/whale/manifest.json"

    def _gallery(self, character, asset_id=None):
        # Shared-library membership alone says nothing about the depicted character.
        # Only the bundled reactions belong to the bundled whale manifest.
        if not self._whale_manifest(character):
            return []
        rows = self.store.all(
            "SELECT * FROM meme_assets WHERE storage_kind='existing_user_assets' AND instr(blob_name,'.')>0 "
            "AND deleted=0 AND enabled=1 AND (? IS NULL OR asset_id=?) ORDER BY created_at,asset_id",
            (asset_id, asset_id),
        )
        items = []
        for row in rows:
            try:
                asset = self.catalog.resolve(row["asset_id"], character["id"])
            except (ValueError, OSError):
                continue
            asset.pop("path")
            if _attributed(asset):
                items.append(asset | {"preview_url": f"/api/memes/{row['asset_id']}/preview",
                                      "style": "档案馆投稿" if asset["source"]["kind"] == "licensed_pack" else "Q版全身",
                                      "is_default": False})
        return items

    def _lookup(self, character, asset_id):
        items = self._gallery(character, asset_id) if ASSET_ID.fullmatch(asset_id) else self._portraits(character, asset_id)
        return next((item for item in items if item["asset_id"] == asset_id), None)

    def state(self, character_id):
        character = self._character(character_id)
        saved = self.store.get("pet_appearance:" + character_id, {})
        selected = self._lookup(character, saved["asset_id"]) if saved.get("asset_id") else None
        return {"character_id": character_id, "pets_enabled": performance(self.store)["pets_enabled"],
                "selected": selected, "selected_asset_id": selected["asset_id"] if selected else None,
                "revision": saved.get("revision", 0)}

    async def options(self, character_id):
        character = self._character(character_id)
        if self._whale_manifest(character):
            await self.catalog.seed_existing()
        character = self._character(character_id)
        return self.state(character_id) | {"items": self._portraits(character) + self._gallery(character)}

    async def select(self, character_id, asset_id):
        character = self._character(character_id)
        if asset_id is not None:
            if not isinstance(asset_id, str) or len(asset_id) > 80:
                raise ValueError("请选择当前角色的已有宠物资源")
            item = self._lookup(character, asset_id)
            if not item:
                raise ValueError("该宠物资源不在当前角色可用清单")
            if item["is_default"]:
                asset_id = None
        key = "pet_appearance:" + character_id
        previous = self.store.get(key, {})
        if previous.get("asset_id") != asset_id:
            self.store.set(key, {"asset_id": asset_id, "revision": previous.get("revision", 0) + 1})
        return self.state(character_id)

    def set_visible(self, character_id, enabled):
        self._character(character_id)
        if type(enabled) is not bool:
            raise ValueError("宠物显示须为布尔值")
        save_performance(self.store, Performance(pets_enabled=enabled))
        return self.state(character_id)


class PetSelection(StrictModel):
    asset_id: str | None = Field(max_length=80)

