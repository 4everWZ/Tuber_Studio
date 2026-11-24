"""Layer 0 dataclasses and enums for metacognitive self-model, context tree, and scoped memory."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MemoryScope(StrEnum):
    """Strict isolation scope for Layer 1 saved memories and Layer 2 recall."""

    OWNER = "owner"
    PUBLIC = "public"


VALID_MEMORY_SCOPES = frozenset({MemoryScope.OWNER.value, MemoryScope.PUBLIC.value})


def parse_memory_scope(value: str | MemoryScope) -> MemoryScope:
    """Validate and normalize a memory scope into MemoryScope."""
    if isinstance(value, MemoryScope):
        return value
    text = str(value or "").strip()
    if text not in VALID_MEMORY_SCOPES:
        raise ValueError("无效记忆作用域")
    return MemoryScope(text)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """Distilled Layer 1 evergreen memory item strictly scoped to owner or public."""

    id: str
    scope: MemoryScope
    character_id: str
    content: str
    source: str = "manual"
    tags: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0
    active: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", parse_memory_scope(self.scope))
        object.__setattr__(self, "character_id", str(self.character_id or "").strip())
        object.__setattr__(self, "tags", tuple(str(tag) for tag in self.tags if str(tag).strip()))
        object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "updated_at", float(self.updated_at))
        object.__setattr__(self, "active", bool(self.active))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": str(self.scope),
            "character_id": self.character_id,
            "content": self.content,
            "source": self.source,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MemoryItem:
        return cls(
            id=str(data["id"]),
            scope=parse_memory_scope(data["scope"]),
            character_id=str(data.get("character_id") or ""),
            content=str(data["content"]),
            source=str(data.get("source") or "manual"),
            tags=tuple(data.get("tags") or ()),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            active=bool(data.get("active", True)),
        )


@dataclass(frozen=True, slots=True)
class MetacognitiveProfile:
    """Situational self-model describing active persona, channel, intent, and epistemic bounds."""

    character_id: str
    character_name: str
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    self_description: str = ""
    speaking_style: str = ""
    owner_address: str = "你"
    owner_uid: str | None = None
    owner_name: str | None = None
    account_uid: str | None = None
    account_name: str | None = None
    channel: str = "owner_chat"
    scene: str = ""
    task_intent: str = ""
    epistemic_boundaries: tuple[str, ...] = ()
    persona_rules: tuple[str, ...] = ()
    forbidden_self_labels: tuple[str, ...] = ()
    extra_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(str(a) for a in self.aliases if str(a).strip()))
        object.__setattr__(
            self,
            "epistemic_boundaries",
            tuple(str(b) for b in self.epistemic_boundaries if str(b).strip()),
        )
        object.__setattr__(self, "persona_rules", tuple(str(r) for r in self.persona_rules if str(r).strip()))
        object.__setattr__(
            self,
            "forbidden_self_labels",
            tuple(str(label) for label in self.forbidden_self_labels if str(label).strip()),
        )
        object.__setattr__(self, "extra_context", deepcopy(dict(self.extra_context or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "character_id": self.character_id,
            "character_name": self.character_name,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "self_description": self.self_description,
            "speaking_style": self.speaking_style,
            "owner_address": self.owner_address,
            "owner_uid": self.owner_uid,
            "owner_name": self.owner_name,
            "account_uid": self.account_uid,
            "account_name": self.account_name,
            "channel": self.channel,
            "scene": self.scene,
            "task_intent": self.task_intent,
            "epistemic_boundaries": list(self.epistemic_boundaries),
            "persona_rules": list(self.persona_rules),
            "forbidden_self_labels": list(self.forbidden_self_labels),
            "extra_context": deepcopy(self.extra_context),
        }


@dataclass(frozen=True, slots=True)
class ContextNode:
    """Immutable node in a Base -> Branches prompt tree for KV-prefix reuse."""

    node_id: str
    kind: str = "base"
    parent_id: str | None = None
    scope: str = ""
    channel: str = ""
    title: str = ""
    messages: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    token_estimate: int = 0
    degraded: bool = False
    created_at: float = 0.0

    def __post_init__(self) -> None:
        frozen_msgs = tuple(deepcopy(dict(msg)) for msg in self.messages)
        object.__setattr__(self, "messages", frozen_msgs)
        object.__setattr__(self, "metadata", deepcopy(dict(self.metadata or {})))
        if not self.content_hash:
            digest = hashlib.sha256(_canonical_json(list(frozen_msgs)).encode("utf-8")).hexdigest()
            object.__setattr__(self, "content_hash", digest)
        if not self.created_at:
            object.__setattr__(self, "created_at", time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "scope": self.scope,
            "channel": self.channel,
            "title": self.title,
            "messages": [deepcopy(msg) for msg in self.messages],
            "metadata": deepcopy(self.metadata),
            "content_hash": self.content_hash,
            "token_estimate": self.token_estimate,
            "degraded": self.degraded,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ContextTree:
    """Tree of ContextNode objects sharing an immutable Base prefix across child Branches."""

    tree_id: str
    root_id: str
    nodes: dict[str, ContextNode] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.nodes = dict(self.nodes)
        if self.nodes and self.root_id not in self.nodes:
            raise ValueError(f"ContextTree root_id={self.root_id!r} missing from nodes")

    def get(self, node_id: str) -> ContextNode | None:
        return self.nodes.get(node_id)

    def children(self, parent_id: str) -> list[ContextNode]:
        return [node for node in self.nodes.values() if node.parent_id == parent_id]

    def add_node(self, node: ContextNode) -> ContextNode:
        if node.node_id == self.root_id:
            if self.root_id in self.nodes and self.nodes[self.root_id].content_hash != node.content_hash:
                raise ValueError("Immutable Base node cannot be overwritten with different content")
            self.nodes[node.node_id] = node
            return node
        if not node.parent_id or node.parent_id not in self.nodes:
            raise ValueError(f"Parent node {node.parent_id!r} not found in ContextTree")
        if any(str(msg.get("role") or "") == "system" for msg in node.messages):
            raise ValueError("Branch nodes must not add mid-tree system messages (preserves Base KV prefix)")
        self.nodes[node.node_id] = node
        return node

    def replace_branch(self, node: ContextNode) -> ContextNode:
        """Replace a non-root branch node (e.g. after appending multi-turn tool/reply messages)."""
        if node.node_id == self.root_id:
            raise ValueError("Cannot replace immutable Base root node")
        if not node.parent_id or node.parent_id not in self.nodes:
            raise ValueError(f"Parent node {node.parent_id!r} not found in ContextTree")
        if any(str(msg.get("role") or "") == "system" for msg in node.messages):
            raise ValueError("Branch nodes must not add mid-tree system messages (preserves Base KV prefix)")
        self.nodes[node.node_id] = node
        return node

    def lineage(self, node_id: str) -> list[ContextNode]:
        path: list[ContextNode] = []
        seen: set[str] = set()
        cursor: str | None = node_id
        while cursor is not None:
            if cursor in seen:
                raise ValueError("Cycle detected in ContextTree")
            seen.add(cursor)
            node = self.nodes.get(cursor)
            if node is None:
                raise KeyError(f"Unknown ContextNode: {cursor!r}")
            path.append(node)
            if cursor == self.root_id:
                break
            cursor = node.parent_id
        path.reverse()
        if not path or path[0].node_id != self.root_id:
            raise ValueError(f"Node {node_id!r} does not descend from root {self.root_id!r}")
        return path

    def base_prefix(self) -> list[dict[str, Any]]:
        root = self.nodes.get(self.root_id)
        if root is None:
            return []
        return [deepcopy(msg) for msg in root.messages]

    def render_messages(self, node_id: str | None = None) -> list[dict[str, Any]]:
        target = self.root_id if node_id is None else node_id
        rendered: list[dict[str, Any]] = []
        for node in self.lineage(target):
            for msg in node.messages:
                rendered.append(deepcopy(msg))
        return rendered
