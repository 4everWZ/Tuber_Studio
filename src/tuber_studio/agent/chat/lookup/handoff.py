"""Live style presets and re-exported cross-scene handoff validators."""

from ...tasks.handoff import (
    PublicText as PublicText,
)
from ...tasks.handoff import (
    handoff_text as handoff_text,
)
from ...tasks.handoff import (
    supplied_private_fragments as supplied_private_fragments,
)
from ...tasks.handoff import (
    video_findings as video_findings,
)

LIVE_STYLE = {
    "result_first": "先讲结果，再交代过程。",
    "process_first": "先讲过程，再交代结果。",
    "concise": "用简短的一两句话讲清重点。",
    "detailed": "按需要补充能说明结论的细节。",
}
LIVE_STYLE_GROUP = {"result_first": "order", "process_first": "order", "concise": "detail", "detailed": "detail"}
