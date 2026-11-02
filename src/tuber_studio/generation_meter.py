"""Task-local parent-budget admission for actual model generation requests.

Tokenization is read-only for native models. Remote usage probes generate a
token and must reserve a request too. Outside an explicit scope this module is
inert, so live and independent operations keep their existing accounting.
"""

from contextvars import ContextVar
from dataclasses import dataclass

current_meter = ContextVar("generation_meter", default=None)


class GenerationBudgetExceeded(ValueError):
    """The current parent cannot admit another request; retained work can resume."""


def reserve_counters(budget, caps, amounts):
    if any(key not in caps or type(value) is not int or value < 0 for key, value in amounts.items()):
        raise ValueError("无效子任务预算")
    if any(budget[key] + value > caps[key] for key, value in amounts.items()):
        raise GenerationBudgetExceeded("本轮资源预算耗尽，已完成结果保留")
    return budget | {key: budget[key] + value for key, value in amounts.items()}


def settle_counters(budget, field, reserved, actual):
    if actual is None:
        return budget
    if type(reserved) is not int or reserved < 0 or type(actual) is not int or not 0 <= actual <= reserved:
        raise ValueError("模型token计量无效")
    if budget[field] < reserved - actual:
        raise ValueError("模型计量退还超过已预占额度")
    return budget | {field: budget[field] - reserved + actual}


@dataclass
class GenerationReservation:
    meter: "GenerationMeter"
    input_tokens: int
    output_tokens: int
    conservative_input: bool = False
    settled: bool = False


class GenerationMeter:
    def __init__(self, repository, round_id):
        self.repository, self.round_id = repository, round_id
        self.context_token = None

    def __enter__(self):
        self.context_token = current_meter.set(self)
        return self

    def __exit__(self, *exc):
        current_meter.reset(self.context_token)

    def reserve(self, input_tokens, output_tokens, *, conservative_input=False):
        if (
            type(input_tokens) is not int
            or input_tokens < 0
            or type(output_tokens) is not int
            or output_tokens < 1
        ):
            raise ValueError("模型生成预算无效")
        self.repository.reserve_budget(
            self.round_id, model_calls=1, input_tokens=input_tokens, output_tokens=output_tokens
        )
        return GenerationReservation(self, input_tokens, output_tokens, conservative_input)


def reserve_generation(input_tokens, output_tokens, *, conservative_input=False):
    meter = current_meter.get()
    return (
        meter.reserve(input_tokens, output_tokens, conservative_input=conservative_input) if meter else None
    )


async def budgeted_call(provider, callback, messages, *, max_tokens, input_tokens=None):
    """Legacy/custom providers get admission; native HTTP providers meter themselves."""
    if current_meter.get() is None or getattr(provider, "generation_metered", False):
        return await callback()
    count = input_tokens if input_tokens is not None else await provider.count_tokens(messages)
    reservation = reserve_generation(count, max_tokens)
    value = await callback()
    settle_generation(reservation, getattr(provider, "last_usage", {}))
    return value


async def budgeted_json(provider, messages, schema, *, max_tokens, input_tokens=None):
    if current_meter.get() is None or getattr(provider, "generation_metered", False):
        return await provider.generate_json(messages, schema, max_tokens=max_tokens)
    counted = (
        provider.prepare_json_messages(messages, schema, max_tokens=max_tokens)
        if hasattr(provider, "prepare_json_messages")
        else messages
    )
    return await budgeted_call(
        provider,
        lambda: provider.generate_json(messages, schema, max_tokens=max_tokens),
        counted,
        max_tokens=max_tokens,
        input_tokens=input_tokens,
    )


def settle_generation(reservation, usage):
    if reservation is None:
        return
    if reservation.settled:
        raise ValueError("生成计量已结算")
    usage = usage if isinstance(usage, dict) else {}
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if prompt is not None and (type(prompt) is not int or not 0 <= prompt <= reservation.input_tokens):
        raise ValueError("模型输入计量超出父轮预占预算")
    if completion is not None and (
        type(completion) is not int or not 0 <= completion <= reservation.output_tokens
    ):
        raise ValueError("模型输出计量超出父轮预占预算")
    # Native counts already came from the real tokenizer. Remote input admission
    # uses the declared context ceiling until the service returns actual usage.
    if reservation.conservative_input and prompt is not None:
        reservation.meter.repository.settle_input(
            reservation.meter.round_id, reservation.input_tokens, prompt
        )
    reservation.meter.repository.settle_output(
        reservation.meter.round_id, reservation.output_tokens, completion
    )
    reservation.settled = True
