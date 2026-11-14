"""Server-sent events shared by the chat API and the inference client."""

import json


def encode_event(name, value):
    return f"event: {name}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


async def event_data(lines):
    fields = []
    async for line in lines:
        if line.startswith("data:"):
            fields.append(line[5:].removeprefix(" "))
        elif not line and fields:
            yield "\n".join(fields)
            fields.clear()
