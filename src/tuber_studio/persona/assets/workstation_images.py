"""Application-authenticated workstation image jobs with locally approved OC inputs."""
import asyncio
import base64
import io
import re
from typing import Literal

from PIL import Image
from pydantic import Field

from ...core.models import StrictModel
from ...infra.inference.http_api import api_client


class ImageBrief(StrictModel):
    variant: Literal["q", "mature"]
    prompt: str = Field(min_length=1, max_length=3400)


def gateway_base(settings):
    if not settings.provider_key("omni"):
        raise ValueError("工作站图片网关缺少普通应用凭据")
    return settings.data["omni"]["base_url"]


def gateway(settings):
    base = gateway_base(settings)
    return api_client({"base_url": base, "timeout_seconds": 45}, settings.provider_key("omni"))


def image_payload(brief, data, *, character_id):
    if character_id == "whale" and brief.variant == "mature":
        guidance = (
            "Use the mature reference: each view depicts the same woman. Match her face, proportions, "
            "outfit and fin ears. When showing her lower body, draw two human legs, each ending in its "
            "own navy Mary Jane. A separate blue whale tail grows from the back of her pelvis behind "
            "the skirt and ends in two broad horizontal flukes. Stage the pose so each calf and the tail "
            "have a distinct contour with visible space between them. Keep the requested framing. "
            "Draw one scene without sheet panels, labels or duplicate views."
        )
    else:
        guidance = (
            "Use the reference for character design. Multiple views depict the same individual. "
            "Match the head-to-body ratio, facial features, hair and outfit details in the full-body views. "
            "When visible, draw the appendages with the attachment, orientation and silhouette shown "
            "in the detail views. Compose the requested action as one coherent scene without sheet "
            "panels, labels or duplicate views."
        )
    return {
        "prompt": "Draw one finished illustration of this character in the following scene: " + brief.prompt
                  + "\n" + guidance,
        "reference_image_base64": base64.b64encode(data).decode("ascii"),
    }


async def submit(client, payload):
    # A 429 explicitly rejects admission. A transport failure after a POST
    # leaves delivery uncertain and must not be replayed.
    async with asyncio.timeout(1200):
        while True:
            response = await client.post("images/jobs", json=payload)
            if response.status_code != 429:
                response.raise_for_status()
                break
            delay = response.headers.get("Retry-After", "1")
            await asyncio.sleep(max(1, int(delay)) if delay.isdecimal() else 1)
    data = response.json()
    if response.status_code != 202 or not re.fullmatch("[0-9a-f]{32}", str(data.get("id", ""))):
        raise ValueError("工作站未返回有效图片任务回执")
    return data["id"]


async def result_png(client, remote_id):
    async with client.stream("GET", f"images/jobs/{remote_id}/result") as response:
        response.raise_for_status()
        if response.headers.get("content-type", "").partition(";")[0] != "image/png":
            raise ValueError("工作站图片结果格式不符")
        data = bytearray()
        async for part in response.aiter_bytes():
            data.extend(part)
            if len(data) > 8 * 1024**2:
                raise ValueError("图片工件过大")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or image.size != (1024, 1024) or image.n_frames != 1:
            raise ValueError("图片工件尺寸或格式不符")
        image.load()
    return bytes(data)
