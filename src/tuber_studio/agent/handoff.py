"""Prepare and validate the text product that crosses a scene boundary."""

import re
from urllib.parse import urlsplit

from pydantic import Field

from ..models import StrictModel

# Text handoffs never carry a URI or local path. Public video citations are
# added by the owner UI from the recorded source, outside the speech text.
_LOCATION = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.|[a-z]:[\\/]|\\\\|"
    r"(?:^|\s)/(?:[\w.-]+/)+|[\w.+-]+@[\w.-]+\.[a-z]{2,}|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:账号|用户ID|UID)\s*[:：]?\s*\d{4,})"
)
LIVE_STYLE = {
    "result_first": "先讲结果，再交代过程。",
    "process_first": "先讲过程，再交代结果。",
    "concise": "用简短的一两句话讲清重点。",
    "detailed": "按需要补充能说明结论的细节。",
}
LIVE_STYLE_GROUP = {"result_first": "order", "process_first": "order", "concise": "detail", "detailed": "detail"}


class PublicText(StrictModel):
    text: str = Field(max_length=120)
    private_fragments: list[str] = Field(default_factory=list, max_length=12)


def supplied_private_fragments(text):
    return re.findall(r"(?:姓名|真名|名字|昵称|联系人)(?:是|叫|[：:])\s*[「『“\"]?([\w·]{2,32})", text)


def handoff_text(product, *, private_fragments=()):
    text = " ".join(product.text.split())
    if not text or _LOCATION.search(text) or any(
        len(fragment.strip()) >= 2 and fragment.strip().casefold() in text.casefold()
        for fragment in (*product.private_fragments, *private_fragments)
    ):
        raise ValueError("暂时提取不出可公开的文字")
    return text


def video_findings(status, *, part=None, seconds=None):
    findings = []
    bvid = status["selection"]["bvid"]
    if (frame := status.get("inspection")) and frame.get("text") and (
        part in (None, frame["part"]) and (seconds is None or abs(frame["requested_time"] - seconds) <= 15)
    ):
        actual = frame["actual_time"]
        return [{"part": frame["part"], "seconds": actual, "text": frame["text"], "kind": "画面",
                 "url": f"https://www.bilibili.com/video/{bvid}/?p={frame['part']}&t={int(actual)}"}]
    for item in (status.get("coverage") or {}).get("parts", []):
        number = item["part"]
        if part is not None and number != part:
            continue
        candidates = [entry for interval in item["ranges"] for entry in interval.get("highlights", [])]
        if seconds is not None:
            candidates = sorted((h for h in candidates if abs(h["start"] - seconds) <= 15),
                                key=lambda h: abs(h["start"] - seconds))
        elif len(candidates) > 4:
            candidates = [candidates[round(index * (len(candidates) - 1) / 3)] for index in range(4)]
        for entry in candidates:
            link = urlsplit(entry["source_url"])
            if (link.scheme, link.hostname, link.path.rstrip("/")) != ("https", "www.bilibili.com", f"/video/{bvid}"):
                continue
            finding = {"part": number, "seconds": entry["start"], "text": entry["text"],
                       "url": entry["source_url"], "kind": entry["kind"]}
            if finding not in findings:
                findings.append(finding)
    return findings[:12]
