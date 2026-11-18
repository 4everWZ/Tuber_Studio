"""A revocable snapshot of the owner preferences used in one private request."""

from pydantic import Field

from ..daily_memory import MemoryChanged
from ..models import StrictModel
from ..operation_continuations import content_hash


class MemoryUse(StrictModel):
    ids: list[int] = Field(max_length=8)
    stamp: str = Field(pattern=r"^[0-9a-f]{64}$")


def read_stamp(runtime, rows):
    scope = runtime.memory_organizer.scope()
    return content_hash([runtime.tasks.scope(), runtime.daily_memory.preference(scope),
                         [(row["id"], row["revision"]) for row in rows]])


def check_use(runtime, use):
    memory, scope = runtime.daily_memory, runtime.memory_organizer.scope()
    rows = []
    if not memory.preference(scope)["enabled"]:
        raise MemoryChanged("长期记忆已关闭")
    for identity in use.ids:
        matches = memory.organized(scope, item_id=identity)
        if matches and matches[0]["status"] == "active":
            rows.append(matches[0])
    if len(rows) != len(use.ids) or read_stamp(runtime, rows) != use.stamp:
        raise MemoryChanged("引用的记忆或授权已变化，请重新读取")
