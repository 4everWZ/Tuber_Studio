"""Operator-owned live options and the single optional read per text turn."""

import asyncio
import json
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, StrictBool, field_validator

from ..models import Reply, StrictModel
from ..web_fetch import WEB_FETCH_SCHEMA, WebFetchRequest, fetch_excerpt

LOOKUP_TIMEOUT = 2.0
WEATHER_TTL = 600


class WeatherLocation(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class LiveOptions(StrictModel):
    timezone: str | None = Field(default=None, max_length=80)
    web_fetch: StrictBool = False
    weather: WeatherLocation | None = None
    idle: StrictBool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("需要有效的 IANA 时区") from None
        return value


class LiveLookup:
    def __init__(self):
        self.weather_cache = None

    async def weather(self, location):
        key = (location.name, location.latitude, location.longitude)
        if self.weather_cache and self.weather_cache[0] == key and time.monotonic() < self.weather_cache[1]:
            return self.weather_cache[2]
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            async with client.stream(
                "GET",
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "current": "temperature_2m,weather_code",
                },
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=1024):
                    if len(body) + len(chunk) > 8192:
                        raise ValueError("天气响应过大")
                    body.extend(chunk)
        current = json.loads(body)["current"]
        fields = {
            "地点": location.name,
            "数据时间 UTC": str(current["time"])[:25],
            "气温 °C": float(current["temperature_2m"]),
            "天气": weather_description(current["weather_code"]),
            "来源": "Open-Meteo",
        }
        self.weather_cache = key, time.monotonic() + WEATHER_TTL, fields
        return fields

    async def generate(self, provider, messages, event_id, options):
        schema = Reply.model_json_schema()
        schema["properties"]["reply_to"]["items"] = {"type": "string", "enum": [event_id]}
        schema["properties"]["reply_to"]["maxItems"] = 1
        choices, instructions = [schema], []
        if options.web_fetch:
            choices.append(WEB_FETCH_SCHEMA)
            instructions.append('需要看网页原文时选 {"web_fetch":"公开HTTPS网页完整链接"}。')
        if options.weather:
            choices.append({
                "type": "object",
                "properties": {"weather": {"type": "boolean", "enum": [True]}},
                "required": ["weather"],
                "additionalProperties": False,
            })
            instructions.append('需要查设置地点的天气时选 {"weather":true}。')
        maximum = provider.config.get("max_tokens", 160)
        request = await provider.generate_json(
            [*messages, {
                "role": "system",
                "content": "直接回复当前弹幕，或先选一项取材。" + "".join(instructions),
            }],
            {"type": "object", "oneOf": choices},
            max_tokens=maximum,
        )
        if "weather" in request or "web_fetch" in request:
            result = await self.read(options, request)
            request = await provider.generate_json(
                [*messages, {
                    "role": "user",
                    "content": "本轮取材结果："
                    + json.dumps(result, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e"),
                }],
                schema,
                max_tokens=maximum,
            )
        reply = Reply.model_validate(request)
        if any(reference != event_id for reference in reply.reply_to):
            raise ValueError("模型引用了其他事件")
        return reply

    async def read(self, options, request):
        try:
            async with asyncio.timeout(LOOKUP_TIMEOUT):
                if options.weather and request.keys() == {"weather"} and request["weather"] is True:
                    return await self.weather(options.weather)
                if options.web_fetch:
                    url = WebFetchRequest.model_validate(request).web_fetch
                    result = await fetch_excerpt(url)
                    return {"网页": result["url"], "摘录": result["excerpt"]}
        except (TimeoutError, httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, KeyError, TypeError):
            pass
        return {"状态": "没有查到本轮所需资料"}


def weather_description(code):
    if type(code) is not int:
        raise ValueError("天气编码无效")
    for codes, text in (
        ((0,), "晴"), ((1, 2, 3), "云"), ((45, 48), "雾"),
        ((51, 53, 55, 56, 57), "毛毛雨"), ((61, 63, 65, 66, 67, 80, 81, 82), "雨"),
        ((71, 73, 75, 77, 85, 86), "雪"), ((95, 96, 99), "雷雨"),
    ):
        if code in codes:
            return text
    return "未知"
