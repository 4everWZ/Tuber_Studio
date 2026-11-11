"""Optional operator-installed Cubism assets for the existing scene renderer."""

import json
from pathlib import PurePosixPath
from urllib.parse import quote

from pydantic import Field

from ..models import Animation, Emotion, StrictModel

SDK = ("pixi.min.js", "live2dcubismcore.min.js", "cubism4.min.js")
PREFIX = "/media/live2d"


class AvatarMotion(StrictModel):
    group: str = Field(min_length=1, max_length=80)
    index: int = Field(default=0, ge=0, le=100)


class AvatarManifest(StrictModel):
    model: str = Field(min_length=1, max_length=160)
    license: str = Field(default="", max_length=500)
    commercial_allowed: bool = False
    mouth_parameter: str = Field(default="ParamMouthOpenY", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    mouth_gain: float = Field(default=5.0, gt=0, le=10)
    motions: dict[Animation, AvatarMotion] = Field(default_factory=dict)
    expressions: dict[Emotion, str] = Field(default_factory=dict)


def checked_file(root, relative):
    posix = PurePosixPath(relative)
    if (
        not 1 <= len(relative) <= 240 or posix.is_absolute() or ".." in posix.parts
        or any(character in relative for character in ("\\", ":", "?", "#", "%", "\x00"))
        or posix.as_posix() != relative
    ):
        raise ValueError("Live2D 路径无效")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Live2D 文件缺失")
    return path


def load_object(path, limit):
    with path.open("rb") as file:
        raw = file.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Live2D 配置过大")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Live2D 配置需要 JSON 对象")
    return value


def avatar_package(root):
    """Return a local export manifest and its complete runtime file closure."""
    root = root.resolve()
    manifest = AvatarManifest.model_validate(load_object(checked_file(root, "avatar.json"), 32768))
    if not manifest.model.endswith(".model3.json"):
        raise ValueError("需要 Cubism model3.json 模型")
    model = load_object(checked_file(root, manifest.model), 256 * 1024)
    references = model["FileReferences"]
    motions = references.get("Motions", {})
    expressions = references.get("Expressions", [])
    if not references["Moc"].endswith(".moc3") or not references["Textures"]:
        raise ValueError("Live2D 模型缺少 Moc 或贴图")
    for name, motion in manifest.motions.items():
        if motion.index >= len(motions.get(motion.group, ())):
            raise ValueError(f"Live2D 动作映射未导出：{name}")
    expression_names = {item.get("Name") for item in expressions}
    for name, expression in manifest.expressions.items():
        if expression not in expression_names:
            raise ValueError(f"Live2D 表情映射未导出：{name}")
    files = [references["Moc"], *references["Textures"]]
    files += [references[key] for key in ("Physics", "Pose", "DisplayInfo", "UserData") if key in references]
    files += [item["File"] for item in expressions]
    for group in motions.values():
        for item in group:
            if item.get("Sound"):
                raise ValueError("请移除模型动作内的音效，直播只有一个播放器")
            files.append(item["File"])
    if len(files) > 128:
        raise ValueError("Live2D 模型引用过多")
    parents = PurePosixPath(manifest.model).parent
    allowed = {manifest.model}
    for item in files:
        relative = (parents / item).as_posix()
        path = checked_file(root, relative)
        if path.suffix.lower() not in {".json", ".moc3", ".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Live2D 模型引用了不支持的文件")
        allowed.add(relative)
    return manifest, allowed


class LiveAvatar:
    def __init__(self, settings):
        self.root = (settings.runtime / "live2d").resolve()

    def installed(self, character_id):
        root = self.root / character_id
        if root.resolve() != root:
            raise ValueError("Live2D 角色目录不能重定向")
        manifest, allowed = avatar_package(root)
        for name in SDK:
            checked_file(self.root / "sdk", name)
        return root, manifest, allowed

    def projection(self, character_id):
        if not (self.root / character_id / "avatar.json").is_file():
            return None
        try:
            _, manifest, _ = self.installed(character_id)
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            return {"status": "unavailable", "message": "Live2D 模型或 SDK 不完整，使用 GIF"}
        return {
            "status": "available",
            "model": f"{PREFIX}/model/{quote(character_id)}/{quote(manifest.model)}",
            "scripts": [f"{PREFIX}/sdk/{name}" for name in SDK],
            "mouth_parameter": manifest.mouth_parameter,
            "mouth_gain": manifest.mouth_gain,
            "motions": {key: motion.model_dump() for key, motion in manifest.motions.items()},
            "expressions": manifest.expressions,
            "license": manifest.license,
        }

    def file(self, character_id, group, relative):
        if group == "sdk" and relative in SDK:
            self.installed(character_id)
            return checked_file(self.root / "sdk", relative)
        if group == "model":
            identity, _, asset = relative.partition("/")
            if identity == character_id:
                root, _, allowed = self.installed(character_id)
                if asset in allowed:
                    return checked_file(root, asset)
        raise ValueError("Live2D 文件不存在")
