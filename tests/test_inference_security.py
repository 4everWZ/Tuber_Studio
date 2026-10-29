import math

import pytest

from tuber_studio.inference_validation import (
    project_wav,
    validate_llm_request,
    validate_speech_request,
)


@pytest.mark.parametrize(
    "body",
    [
        {"tools": [{"type": "function", "function": {"name": "read_file"}}]},
        {"functions": [{"name": "shell"}]},
        {"tool_choice": "auto"},
        {"command": "echo secret"},
        {"path": "/etc/passwd"},
        {"image_data": [{"url": "https://example.invalid/image"}]},
        {"chat_template": "an arbitrary template"},
        {"chat_template_kwargs": {"tools": [{"name": "read_file"}]}},
        {"messages": [{"role": "tool", "content": "anything"}]},
        {"messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "file:///etc/passwd"}}],
                }
            ]
        },
        {"messages": [{"role": [], "content": "plain"}]},
        {"reasoning_format": []},
        {"response_format": {"type": []}},
        {"temperature": math.nan},
    ],
)
def test_llm_rejects_capability_fields_and_structured_media(body):
    with pytest.raises(ValueError):
        validate_llm_request("v1/chat/completions", body)


def test_plain_text_can_discuss_commands_paths_and_tools_without_executing_them():
    text = "解释 echo /etc/passwd、rm -rf 和 read_file 的含义，不执行。https://example.invalid"
    body = {
        "model": "MiniCPM5-2B",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 1024,
        "temperature": 1,
        "chat_template_kwargs": {"enable_thinking": True},
        "reasoning_format": "deepseek",
        "response_format": {"type": "json_object", "schema": {"properties": {"tools": {"type": "string"}}}},
    }
    validate_llm_request("v1/chat/completions", body)
    assert body["messages"][0]["content"] == text


def test_native_capacity_probe_and_tokenization_stay_data_only():
    validate_llm_request(
        "completion",
        {"prompt": [1, 2, 3], "n_predict": 4, "ignore_eos": True, "cache_prompt": False, "stream": False},
    )
    validate_llm_request("tokenize", {"content": "file:///etc/passwd is only text", "add_special": False})
    validate_llm_request(
        "apply-template",
        {
            "messages": [{"role": "user", "content": "hello"}],
            "add_generation_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    with pytest.raises(ValueError):
        validate_llm_request("completion", {"prompt": {"path": "/etc/passwd"}})
    with pytest.raises(ValueError):
        validate_llm_request("warmup", {"command": "anything"})


@pytest.mark.parametrize("window,budget", [(4096, 4096), (65536, 65536), (262144, 200000)])
def test_output_budget_uses_requested_context_and_preserves_data_only_boundary(window, budget):
    validate_llm_request("completion", {"prompt": "hello", "n_predict": budget}, context_tokens=window)
    with pytest.raises(ValueError, match="configured context"):
        validate_llm_request(
            "completion", {"prompt": "hello", "n_predict": window + 1}, context_tokens=window
        )
    with pytest.raises(ValueError):
        validate_llm_request("v1/chat/completions", {"tools": [{"name": "shell"}]}, context_tokens=window)


def test_output_schema_can_describe_url_text_but_has_no_remote_resolver():
    schema = {
        "$defs": {"reply": {"type": "string", "description": "Discuss file:///etc/passwd as text"}},
        "$ref": "#/$defs/reply",
    }
    validate_llm_request(
        "v1/chat/completions", {"response_format": {"type": "json_object", "schema": schema}}
    )
    for reference in ("file:///etc/passwd", "https://example.invalid/schema.json"):
        with pytest.raises(ValueError, match="local references"):
            validate_llm_request(
                "v1/chat/completions",
                {"response_format": {"type": "json_object", "schema": {"$ref": reference}}},
            )
    nested = {"type": "string"}
    for _ in range(40):
        nested = {"items": nested}
    with pytest.raises(ValueError, match="complexity"):
        validate_llm_request(
            "v1/chat/completions", {"response_format": {"type": "json_object", "schema": nested}}
        )


@pytest.mark.parametrize(
    "body",
    [
        {"weights_path": "/etc/passwd"},
        {"save_path": "/home/other-user/output.wav"},
        {"command": "restart"},
        {"aux_ref_audio_paths": ["https://example.invalid/reference.wav"]},
        {"media_type": "ogg"},
        {"text_split_method": {"command": "anything"}},
    ],
)
def test_speech_has_no_control_or_arbitrary_file_operation(body):
    with pytest.raises(ValueError):
        validate_speech_request(body)


def test_wav_reference_rejects_links_and_traversal_even_when_target_is_a_wave(tmp_path):
    allowed = tmp_path / "assets/oc/ref.wav"
    allowed.parent.mkdir(parents=True)
    allowed.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    assert project_wav(tmp_path, "assets/oc/ref.wav") == allowed
    with pytest.raises(ValueError):
        project_wav(tmp_path, "assets/oc/../oc/ref.wav")
    with pytest.raises(ValueError):
        project_wav(tmp_path, "file:///etc/passwd")
    alias = allowed.with_name("alias.wav")
    try:
        alias.symlink_to(allowed)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError):
        project_wav(tmp_path, str(alias))


def test_entire_reference_directory_cannot_link_outside_project(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    reference = outside / "ref.wav"
    reference.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    try:
        (tmp_path / "assets").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ValueError):
        project_wav(tmp_path, "assets/ref.wav")
