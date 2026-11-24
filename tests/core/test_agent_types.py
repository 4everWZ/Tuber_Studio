"""Unit tests for L0 core agent dataclasses and immutable ContextTree invariants."""

import json

import pytest

from tuber_studio.core.agent_types import (
    ContextNode,
    ContextTree,
    MemoryItem,
    MemoryScope,
    MetacognitiveProfile,
    parse_memory_scope,
)
from tuber_studio.core.models import (
    ContextNode as ModelsContextNode,
)
from tuber_studio.core.models import (
    ContextTree as ModelsContextTree,
)
from tuber_studio.core.models import (
    MemoryItem as ModelsMemoryItem,
)
from tuber_studio.core.models import (
    MemoryScope as ModelsMemoryScope,
)
from tuber_studio.core.models import (
    MetacognitiveProfile as ModelsMetacognitiveProfile,
)


def test_core_models_reexport_agent_types():
    assert ModelsMemoryScope is MemoryScope
    assert ModelsMemoryItem is MemoryItem
    assert ModelsMetacognitiveProfile is MetacognitiveProfile
    assert ModelsContextNode is ContextNode
    assert ModelsContextTree is ContextTree


def test_memory_scope_and_item_roundtrip():
    assert MemoryScope.OWNER == "owner"
    assert MemoryScope.PUBLIC == "public"
    assert parse_memory_scope("owner") is MemoryScope.OWNER
    assert parse_memory_scope(MemoryScope.PUBLIC) is MemoryScope.PUBLIC
    with pytest.raises(ValueError):
        parse_memory_scope("admin")

    item = MemoryItem(
        id="m1",
        scope="owner",
        character_id="whale",
        content="喜欢晚上喝热牛奶",
        tags=("偏好", "  ", "日常"),
        created_at=100,
        updated_at=200,
    )
    assert item.scope is MemoryScope.OWNER
    assert item.tags == ("偏好", "日常")
    as_dict = item.to_dict()
    assert as_dict["scope"] == "owner"
    assert as_dict["tags"] == ["偏好", "日常"]
    assert MemoryItem.from_dict(as_dict) == item


def test_metacognitive_profile_is_immutable_and_serializable():
    extra = {"speaker": {"uid": "42", "name": "Alice"}}
    profile = MetacognitiveProfile(
        character_id="whale",
        character_name="小鲸鱼",
        aliases=("鱼鱼", "  "),
        epistemic_boundaries=("分清自己与别人",),
        extra_context=extra,
    )
    extra["speaker"]["name"] = "Mutated"
    assert profile.aliases == ("鱼鱼",)
    assert profile.extra_context["speaker"]["name"] == "Alice"
    assert profile.to_dict()["character_name"] == "小鲸鱼"


def test_context_tree_enforces_immutable_base_and_byte_identical_prefix():
    base_messages = (
        {"role": "system", "content": "你是小鲸鱼。"},
        {"role": "user", "content": "视频标题：测试视频"},
    )
    base = ContextNode(node_id="base-1", kind="base", messages=base_messages)
    tree = ContextTree(tree_id="t1", root_id=base.node_id, nodes={base.node_id: base})

    b1 = ContextNode(
        node_id="branch-top",
        kind="branch",
        parent_id=base.node_id,
        messages=({"role": "user", "content": "写一条主评"},),
    )
    b2 = ContextNode(
        node_id="branch-floor-99",
        kind="branch",
        parent_id=base.node_id,
        messages=(
            {"role": "user", "content": "[评论者 · 张三] 第一句"},
            {"role": "assistant", "content": "收到"},
            {"role": "user", "content": "[评论者 · 张三] 追问"},
        ),
    )
    tree.add_node(b1)
    tree.add_node(b2)

    msgs1 = tree.render_messages(b1.node_id)
    msgs2 = tree.render_messages(b2.node_id)
    prefix_len = len(base.messages)
    assert json.dumps(msgs1[:prefix_len], ensure_ascii=False) == json.dumps(
        msgs2[:prefix_len], ensure_ascii=False
    )

    # Mutating returned message lists must not alter stored base node
    msgs1[0]["content"] = "TAMPERED"
    assert tree.base_prefix()[0]["content"] == "你是小鲸鱼。"

    # Mid-tree system messages are forbidden in branches
    bad_branch = ContextNode(
        node_id="bad",
        kind="branch",
        parent_id=base.node_id,
        messages=({"role": "system", "content": "覆盖系统提示"},),
    )
    with pytest.raises(ValueError, match="system"):
        tree.add_node(bad_branch)

    # Replacing root with different content fails
    mutated_base = ContextNode(
        node_id=base.node_id,
        kind="base",
        messages=({"role": "system", "content": "CHANGED"},),
    )
    with pytest.raises(ValueError, match="Immutable Base"):
        tree.add_node(mutated_base)
