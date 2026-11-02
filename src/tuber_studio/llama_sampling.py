"""Explicit llama.cpp sampling options and the existing local-provider defaults."""

from pydantic import BaseModel, ConfigDict, Field


class LlamaCppSampling(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    temperature: float | None = Field(default=0.2, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=2**31 - 1)
    min_p: float | None = Field(default=None, ge=0, le=1)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    repeat_penalty: float | None = Field(default=None, gt=0)
    seed: int | None = Field(default=None, ge=-1, le=2**32 - 1)
    reasoning_budget_tokens: int | None = Field(default=None, ge=-1, le=2**31 - 1)


LOCAL_DEFAULTS = {
    "temperature": 0.7,
    "top_p": 0.95,
    "min_p": 0.0,
    "top_k": 40,
    "presence_penalty": 0.0,
    "repeat_penalty": 1.0,
}


def llama_cpp_sampling(config, *, summary=False, local_defaults=False):
    """Keep native defaults unchanged; API mode forwards only explicit values.

    Native callers retain their existing validation and separate seed handling.
    New public video/API configurations validate with LlamaCppSampling.
    """
    keys = LOCAL_DEFAULTS if local_defaults else LlamaCppSampling.model_fields
    values = (
        {key: config.get(key, LOCAL_DEFAULTS[key]) for key in keys}
        if local_defaults
        else {key: config[key] for key in keys if config.get(key) is not None}
    )
    if summary:
        values.pop("temperature", None)
        if local_defaults or config.get("summary_temperature") is not None:
            values["temperature"] = config.get("summary_temperature", 0.2)
    return values
