"""Data-only inference requests; this is not an arbitrary-code execution sandbox.

The operating-system boundary is the non-privileged, read-only container. These
checks keep its internal servers' wider file/tool APIs out of the broker surface.
Ordinary prompt text is never interpreted as a command or inspected for keywords.
"""

import math
from pathlib import Path

GENERATION_FIELDS = {
    "model",
    "seed",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "n_predict",
    "stream",
    "cache_prompt",
    "response_format",
    "chat_template_kwargs",
    "reasoning_format",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "stop",
    "ignore_eos",
}
SPEECH_FIELDS = {
    "text",
    "text_lang",
    "ref_audio_path",
    "aux_ref_audio_paths",
    "prompt_lang",
    "prompt_text",
    "top_k",
    "top_p",
    "temperature",
    "text_split_method",
    "batch_size",
    "batch_threshold",
    "split_bucket",
    "speed_factor",
    "fragment_interval",
    "seed",
    "media_type",
    "streaming_mode",
    "parallel_infer",
    "repetition_penalty",
}


def _plain_messages(messages):
    if not isinstance(messages, list):
        raise ValueError("Messages must contain plain text only")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in ("system", "user", "assistant")
            or not isinstance(message["content"], str)
        ):
            raise ValueError("Only plain system, user and assistant text is supported")


def validate_llm_request(path, body):
    if path == "warmup":
        allowed = set()
    elif path == "tokenize":
        allowed = {"content", "add_special", "parse_special", "with_pieces"}
    elif path == "apply-template":
        allowed = {"model", "messages", "add_generation_prompt", "chat_template_kwargs"}
    else:
        allowed = GENERATION_FIELDS | ({"messages"} if path == "v1/chat/completions" else {"prompt"})
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("Unsupported text inference fields; tools, files and remote media are disabled")
    if "messages" in body:
        _plain_messages(body["messages"])
    if "content" in body and not isinstance(body["content"], str):
        raise ValueError("Tokenization accepts plain text only")
    if "prompt" in body:
        prompt = body["prompt"]
        if not isinstance(prompt, str) and not (
            isinstance(prompt, list) and all(type(token) is int and token >= 0 for token in prompt)
        ):
            raise ValueError("Completion accepts plain text or a single token sequence only")
    if "chat_template_kwargs" in body:
        options = body["chat_template_kwargs"]
        if not isinstance(options, dict) or set(options) - {"enable_thinking"}:
            raise ValueError("Only the fixed model's thinking option is supported")
        if "enable_thinking" in options and type(options["enable_thinking"]) is not bool:
            raise ValueError("Thinking option must be a boolean")
    if body.get("reasoning_format", "deepseek") not in ("none", "deepseek", "auto"):
        raise ValueError("Unsupported reasoning format")
    if "response_format" in body:
        response = body["response_format"]
        if (
            not isinstance(response, dict)
            or set(response) - {"type", "schema", "json_schema"}
            or response.get("type") not in ("text", "json_object", "json_schema")
        ):
            raise ValueError("Unsupported response format")
        for key in ("schema", "json_schema"):
            if key in response and not isinstance(response[key], dict):
                raise ValueError("Response schema must be an object")
            if key in response:
                _local_schema(response[key])
    for key in ("max_tokens", "n_predict"):
        if key in body and (type(body[key]) is not int or not 1 <= body[key] <= 131072):
            raise ValueError("Output token budget must fit the fixed context window")
    _finite_numbers(body)


def validate_speech_request(body):
    if not isinstance(body, dict) or set(body) - SPEECH_FIELDS:
        raise ValueError("Unsupported speech fields; arbitrary files and control operations are disabled")
    for key in ("text", "text_lang", "ref_audio_path", "prompt_lang", "prompt_text"):
        if key in body and not isinstance(body[key], str):
            raise ValueError("Speech text, language and configured reference must be strings")
    if body.get("media_type", "wav") != "wav" or body.get("streaming_mode", False) is not False:
        raise ValueError("Only buffered WAV speech is supported")
    if body.get("aux_ref_audio_paths"):
        raise ValueError("Auxiliary reference files are disabled")
    if "text_split_method" in body and body["text_split_method"] not in tuple(f"cut{i}" for i in range(6)):
        raise ValueError("Unsupported speech segmentation method")
    _finite_numbers(body)


def _finite_numbers(body):
    for value in body.values():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Inference parameters must be finite")


def _local_schema(schema):
    # Schemas can describe ordinary URLs as data, but cannot acquire a resolver
    # capable of reading URLs/files. Bound nesting before the native converter.
    pending = [(schema, 0)]
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if depth > 32 or visited > 4096:
            raise ValueError("Response schema exceeds the supported complexity")
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef", "$id"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    raise ValueError("Response schemas support local references only")
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def project_wav(root, value):
    """Resolve an existing WAV without following links or escaping fixed assets."""
    if not isinstance(value, str) or not value or "://" in value:
        raise ValueError("Reference must be a configured project WAV")
    root = root.resolve()
    raw = Path(value)
    if ".." in raw.parts:
        raise ValueError("Reference traversal is disabled")
    path = raw if raw.is_absolute() else root / raw
    if not path.is_relative_to(root):
        raise ValueError("Reference must stay inside this project")
    for component in [path, *path.parents]:
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("Reference links are disabled")
    path = path.resolve(strict=True)
    allowed = (root / "assets", root / "runtime/tts/ref")
    if (
        not any(path.is_relative_to(base) for base in allowed)
        or path.suffix.lower() != ".wav"
        or not path.is_file()
    ):
        raise ValueError("Reference must be an existing project WAV")
    with path.open("rb") as audio:
        header = audio.read(12)
    if header[:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
        raise ValueError("Reference must be a WAV file")
    return path
