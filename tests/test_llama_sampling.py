import pytest

from tuber_studio.llama_sampling import LlamaCppSampling, llama_cpp_sampling


@pytest.mark.parametrize(
    "patch",
    [
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": float("nan")},
        {"temperature": True},
        {"top_p": 1.1},
        {"min_p": -0.1},
        {"top_k": -1},
        {"top_k": 20.0},
        {"top_k": True},
        {"presence_penalty": 2.1},
        {"repeat_penalty": 0},
        {"repeat_penalty": float("inf")},
        {"seed": -2},
        {"seed": 2**32},
        {"seed": True},
        {"reasoning_budget_tokens": -2},
        {"reasoning_budget_tokens": 1024.0},
        {"reasoning_budget_tokens": True},
        {"tools": []},
    ],
)
def test_explicit_sampling_rejects_invalid_types_ranges_and_controls(patch):
    with pytest.raises(ValueError):
        LlamaCppSampling(**patch)


def test_supported_disable_values_and_service_defaults_are_preserved():
    parsed = LlamaCppSampling(
        temperature=None,
        top_p=1,
        top_k=0,
        min_p=0,
        presence_penalty=-2,
        repeat_penalty=1,
        seed=-1,
        reasoning_budget_tokens=-1,
    )
    assert llama_cpp_sampling(parsed.model_dump()) == {
        "top_p": 1,
        "top_k": 0,
        "min_p": 0,
        "presence_penalty": -2,
        "repeat_penalty": 1,
        "seed": -1,
        "reasoning_budget_tokens": -1,
    }


def test_native_defaults_summary_and_existing_seed_placement_are_unchanged():
    config = {"seed": 42, "temperature": 0.8, "summary_temperature": 0.1}
    expected = {
        "temperature": 0.8,
        "top_p": 0.95,
        "min_p": 0.0,
        "top_k": 40,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
    }
    assert llama_cpp_sampling(config, local_defaults=True) == expected
    assert llama_cpp_sampling(config, summary=True, local_defaults=True) == expected | {"temperature": 0.1}
    assert llama_cpp_sampling({}, summary=True, local_defaults=True) == expected | {"temperature": 0.2}
    # Legacy native configuration keeps its established validation behavior.
    assert llama_cpp_sampling({"temperature": None}, local_defaults=True)["temperature"] is None
