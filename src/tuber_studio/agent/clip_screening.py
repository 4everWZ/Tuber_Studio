"""Omni screening of ordered clip pixels, with one bound observation per image."""

import json
from typing import Literal

from pydantic import Field

from ..compaction import ContextWindowExceeded
from ..http_api import InferenceBudgetExceeded
from ..models import StrictModel
from .task_delivery import DelegationError


class WindowObservation(StrictModel):
    scene: Literal["符合要求", "不符合要求", "无法核实"]
    captions: Literal["无烧录字幕", "已读且符合", "已读但不符合", "无法读清"]
    screen_text: str = Field(description="抄录这张画面上实际可读的屏字，包括能辨明的标识，不补齐断句。")
    basis: str = Field(min_length=1, description="写出本帧可见的依据和它是否触及号主要求的素材限制。")


async def fit_review_group(keys, request, limits):
    """Only an actual context/admission limit permits splitting the same inputs."""
    try:
        return await request(keys)
    except (InferenceBudgetExceeded, ContextWindowExceeded, DelegationError) as error:
        limit = (InferenceBudgetExceeded, ContextWindowExceeded)
        cause = error if isinstance(error, limit) else error.__cause__
        if len(keys) == 1 or not isinstance(cause, limit):
            raise
        limits.append({"keys": list(keys), "error": type(cause).__name__, "message": str(cause)})
    mid = len(keys) // 2
    left = await fit_review_group(keys[:mid], request, limits)
    return left | await fit_review_group(keys[mid:], request, limits)


_SCREEN = (
    "你负责短片的画面和烧录字幕审查。按原帧编号分别填写判断。相邻图片可以帮助区分图形、"
    "投稿标识、节目介绍及片内叙事，但每帧的文字和事实必须来自该帧。"
    "scene 核对本帧画面是否违反号主对素材内容的限制；片段数量、查出处和整份提纲由编辑完成。"
    "号主未要求必须有字幕时，无字幕本身不构成画面不合格。"
    "逐张读取底部及画内其它位置的烧录文字，区分投稿水印与节目字幕。"
    "有节目字幕而读不清须标无法读清；无法判断画面须标无法核实。"
    "若要求不剧透，具体剧情的起因、事件、结果或角色台词均须拒绝；"
    "节目引介、收视评价和制作资料单独判断。只核实画面。"
)


async def observe_clip_batch(samples, images, goal, generate, budget, calls, limits):
    indexed = {sample["frame"]: (sample, image) for sample, image in zip(samples, images, strict=True)}

    async def request(keys):
        content = []
        for frame in keys:
            sample, image = indexed[frame]
            content.extend(
                (
                    {
                        "type": "text",
                        "text": json.dumps(
                            {"frame": frame, "seconds": sample["seconds"]}, ensure_ascii=False
                        ),
                    },
                    image,
                )
            )
        schema = WindowObservation.model_json_schema()
        if len(keys) > 1:
            schema = {
                "type": "object",
                "properties": {str(frame): schema for frame in keys},
                "required": list(map(str, keys)),
                "additionalProperties": False,
            }
        result = await generate(
            [
                {"role": "system", "content": _SCREEN},
                {"role": "user", "content": [*content, {"type": "text", "text": goal}]},
            ],
            schema,
            max_tokens=budget,
        )
        if len(keys) == 1:
            result = {str(keys[0]): result}
        if set(result) != set(map(str, keys)):
            raise DelegationError("视觉审查未逐帧绑定送审原图。")
        checked = {k: WindowObservation.model_validate(result[str(k)]).model_dump() for k in keys}
        calls.append({"first_frame": keys[0], "last_frame": keys[-1], "count": len(keys)})
        return checked

    # Start later batches below observed refusals. Every image still gets its
    # own frame-bound verdict, even on a service admitting just one at a time.
    keys = list(indexed)
    replies = {}
    while keys:
        size = min((len(hit["keys"]) // 2 for hit in limits), default=len(keys))
        replies.update(await fit_review_group(keys[:size], request, limits))
        keys = keys[size:]
    return [sample | replies[sample["frame"]] for sample in samples]
