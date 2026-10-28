import json

import httpx
import pytest

from tuber_studio.providers import LlamaCppProvider


@pytest.mark.parametrize(
    "properties,reason",
    [
        ({"default_generation_settings": {"n_ctx": 3072}}, "小于配置"),
        ({"default_generation_settings": {"n_ctx": 131072}, "model_alias": "other-model"}, "别名不一致"),
    ],
)
async def test_wrong_server_capacity_or_model_is_rejected_before_generation(
    settings, store, properties, reason
):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=properties)

    provider = LlamaCppProvider(settings.data["llm"])
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(ValueError, match=reason):
            await provider.generate([], store.character(), "event")
        assert requests == ["/props"]
        assert provider.profile_error
    finally:
        await provider.close()


async def test_structured_decoder_constrains_reply_to_current_event(settings, store):
    def respond(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 131072}})
        if request.url.path == "/apply-template":
            return httpx.Response(200, json={"prompt": "rendered"})
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [1, 2, 3]})
        data = json.loads(request.content)
        schema = data["response_format"]["schema"]
        assert schema["properties"]["reply_to"]["items"]["enum"] == ["comment:exact"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "spoken_text": "今天当然要来一碗白饭。",
                                    "emotion": "cheerful",
                                    "animation_id": "talk_happy",
                                    "reply_to": ["comment:exact"],
                                }
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = LlamaCppProvider(settings.data["llm"])
    await provider.client.aclose()
    provider.client = httpx.AsyncClient(base_url="http://model", transport=httpx.MockTransport(respond))
    try:
        reply = await provider.generate([], store.character(), "comment:exact")
        assert reply.reply_to == ["comment:exact"]
        assert provider.last_tokens == 3
        with pytest.raises(ValueError, match="复述了输入"):
            await provider.generate([], store.character(), "comment:exact", source_text=reply.spoken_text)
    finally:
        await provider.close()
