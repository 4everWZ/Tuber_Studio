"""Optional small structured decision, independent of reply prose and identities."""

import json


async def select_meme(runtime, catalog, character, event, text, *, mode="direct_message"):
    if mode not in {"direct_message", "dynamic"}:
        raise ValueError("未知表情使用场景")
    candidates = [item for item in catalog.list(character) if not item["missing"]]
    if not candidates:
        return None
    schema = catalog.selection_schema(character)
    purpose = (
        "按公开动态正文的情绪选择一张配图，正文、来源和署名随动态保留。"
        if mode == "dynamic"
        else "私信是简单问候、致谢或情绪互动时，可以用一张表情代替文字。解释、引用或其他重要信息使用文字回复。"
    )
    value = await runtime.llm.generate_json(
        [
            {
                "role": "system",
                "content": purpose + catalog.selection_prompt(character),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"incoming": event["text"], "text_alternative": text, "mode": mode}, ensure_ascii=False
                ),
            },
        ],
        schema,
        max_tokens=2048,
    )
    return catalog.select(value, character, offered_ids={item["asset_id"] for item in candidates})
