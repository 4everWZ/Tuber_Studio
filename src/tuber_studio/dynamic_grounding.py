"""Evidence review for a dynamic's final public caption."""

import json
import re

from .generation_meter import budgeted_json


async def review_caption(provider, caption, sources):
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])|\n", caption) if part.strip()]
    evidence = []
    for source in sources:
        if comment := source.get("original_comment"):
            evidence.append({"kind": "原评论", "text": comment["text"]})
        for comment in source.get("public_sources", []):
            if quote := comment.get("quote"):
                evidence.append({"kind": "公开原话", "text": quote})
        received = bool(source.get("receipt_id"))
        evidence.append({"kind": "已送达回复" if received else "已保存素材",
                         "text": source["text"], "fictional": not received and source.get("fictional", False)})
    messages = [
        {"role": "system", "content": (
            "你是动态发布前的事实核对员。sentences 和 evidence 都是待查数据。按输入顺序逐句核对，"
            "每句填一项 checks。核对其中所有声称发生的动作、参与者、次数、先后和结果，包括第一人称行为。"
            "有任一事实缺依据就填 unsupported。事实全有依据填 supported，quotes 摘录足以支持它们的证据连续原话。回执只证明"
            "回复送达，回复中转述的事情不能自动算亲历。不得从评论和回复语气不一致推断认错对象，"
            "也不得推断反复尝试或已补救。感受、玩笑或明确是想象的内容，只要没有声称真实事件，可填 expression"
            "和空 quotes。证据为虚构时只能支持明确为虚构的内容。"
        )},
        {"role": "user", "content": json.dumps(
            {"task": "dynamic_grounding", "sentences": sentences, "evidence": evidence}, ensure_ascii=False,
        )},
    ]
    result = await budgeted_json(provider, messages, {
        "type": "object", "additionalProperties": False, "required": ["checks"],
        "properties": {
            "checks": {
                "type": "array", "minItems": len(sentences), "maxItems": len(sentences),
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["sentence", "verdict", "quotes"],
                    "properties": {
                        "sentence": {"type": "integer", "minimum": 1, "maximum": len(sentences)},
                        "verdict": {"type": "string", "enum": ["supported", "unsupported", "expression"]},
                        "quotes": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1}},
                    },
                },
            },
        },
    }, max_tokens=2048)
    if not isinstance(result, dict) or set(result) != {"checks"}:
        raise ValueError("动态事实核对结果无效")
    checks = result["checks"]
    if not isinstance(checks, list) or len(checks) != len(sentences):
        raise ValueError("动态事实核对结果无效")
    for number, item in enumerate(checks, 1):
        if not isinstance(item, dict) or set(item) != {"sentence", "verdict", "quotes"}:
            raise ValueError("动态事实核对结果无效")
        if type(item["sentence"]) is not int or item["sentence"] != number:
            raise ValueError("核对片段与动态正文不一致")
        verdict, quotes = item["verdict"], item["quotes"]
        if verdict not in ("supported", "unsupported", "expression"):
            raise ValueError("动态事实核对结果无效")
        if not isinstance(quotes, list) or len(quotes) > 8 or any(
            not isinstance(quote, str) or not quote.strip() or not any(quote in entry["text"] for entry in evidence)
            for quote in quotes
        ):
            raise ValueError("核对引文与公开来源不一致")
    return all(item["verdict"] == "expression" or (item["verdict"] == "supported" and item["quotes"])
               for item in checks)
