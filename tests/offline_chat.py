"""Deterministic chat double for isolated business-flow tests."""

from tuber_studio.compaction import approx_token_count
from tuber_studio.models import Reply
from tuber_studio.operation_identity import provider_identity_messages


class OfflineChat:
    """Explicit offline diagnostic only; never labelled as real inference."""

    supports_memory_organization = False
    supports_video_research = False
    last_tokens = None
    server_tokens = None
    profile_error = "离线诊断，无真实128K模型能力"
    is_sleeping = None

    async def close(self):
        pass

    async def health(self):
        return {"available": True, "detail": "离线固定台词，不是模型推理"}

    async def count_tokens(self, messages):
        return sum(approx_token_count(m["content"]) + 8 for m in provider_identity_messages(self, messages))

    async def prefill(self, messages):
        return False

    async def summarize(self, messages, *, max_tokens=None):
        raise ValueError("离线诊断Provider没有模型摘要能力，请连接真实LLM")

    async def generate_json(self, messages, schema, *, max_tokens):
        raise ValueError("离线诊断Provider没有模型结构化生成能力，请连接真实LLM")

    async def generate(self, messages, character, event_id, source_text=None):
        return Reply(
            spoken_text="你好呀，先来一碗白饭，再陪你聊聊天。",
            emotion="cheerful",
            animation_id="talk_happy",
            reply_to=[event_id],
        )

    async def generate_operation(self, messages, character, event_id, source_text=None):
        return await self.generate(messages, character, event_id, source_text)
