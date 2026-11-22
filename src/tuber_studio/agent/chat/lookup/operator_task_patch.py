"""Editable coordinates and owner-quoted changes for one private task card."""
import re
from difflib import SequenceMatcher

from pydantic import Field, PrivateAttr

from ....core.models import StrictModel
from ....core.store import dump


class TaskPatch(StrictModel):
    _item_states: dict[str, str] = PrivateAttr(default_factory=dict)
    field: str = Field(description="选该卡 editable 中的键")
    subject: str = Field(description="逐字摘取号主所指的工作、条件或项目")
    requirement: str = Field(description="逐字摘取号主要求实际执行的新内容或停止指令")
    replacement_goal: str = Field(default="", description="仅在整项目标被替换时逐字摘取完整的新委托。局部改动、补充或依赖原要求时留空。")


def editable_fields(runtime, task, history):
    fields = {"goal": task["goal"]}
    if task["status"] in {"queued", "running", "waiting", "scheduled", "needs_input", "awaiting_approval", "paused"}:
        fields["execution"] = "当前任务的执行状态"
    # Only the exact current, readable delivery can supply item coordinates.
    edition = runtime.tasks.workspace.latest(task["id"])
    if not edition or edition["task_revision"] != task["revision"] or not history.read(task["id"], {
        key: edition[key] for key in ("task_revision", "edition")
    }):
        return {"editable": fields}
    if edition["author"] != "agent":
        return {"editable": fields}
    selected = (task.get("plan") or {}).get("selected") or {}
    for index, item in enumerate(selected.get("items", []), 1):
        title = item.get("title") if isinstance(item, dict) else None
        if isinstance(title, str) and title and title in edition["body"]:
            fields[f"item:{index}"] = title
    return {"editable": fields, "editable_edition": {key: edition[key] for key in ("task_revision", "edition")}}


# Validate a claimed coordinate, after Omni has selected an edit. These are
# references to work and ordered items, independent of the requested operation.
_NUMBER = "一二三四五六七八九十"
_ORDINAL = re.compile(r"第\s*(\d+|[一二三四五六七八九十]+)(?:个|段|项|份|条|张|章|部分)?")
_WORK = re.compile(r"(这|那|当前|刚才|原来|原先|手头|正在做|刚交办)的?(一?[份项个次]|整份)?(工作|任务|交办|提纲|稿|计划|要求|选片|版本)|这[份项]|整[份项]|本次任务")
_FIRST = re.compile(r"(?:开头|头|前|首)(?:一[个段项份]?|半[份段部]?|面(?:那[个段项])?|个|段|项|份)")
_LAST = re.compile(r"(?:最后|后|末|尾)(?:一[个段项份]?|半[份段部]?|面(?:那[个段项])?|个|段|项|份)")
_OTHER = re.compile(r"(?:另(?:外)?一?[段个项份]|其余的?|剩下的?)")
_GOAL_CHANGE = re.compile(r"^\s*(?:改[成为]|调整[成为]|换[成为])\s*")


def complete_replacement(patch, message):
    """Only a complete owner-quoted replacement can supersede the original goal."""
    if not patch or patch.field != "goal":
        return ""
    candidate = patch.replacement_goal.strip()
    if (_GOAL_CHANGE.match(candidate) and candidate == patch.requirement.strip()
            and message.rstrip(" \n。！？!?").endswith(candidate)):
        return candidate
    return ""


def _number(value):
    if value.isdecimal():
        return int(value) if len(value) < 10 else -1
    tens, sep, units = value.partition("十")
    if sep:
        return (1 if not tens else _NUMBER.find(tens) + 1) * 10 + (_NUMBER.find(units) + 1 if units else 0)
    return _NUMBER.find(value) + 1


def _scope_word(word):
    # Polite boilerplate and bare counts do not identify an editable object.
    return word not in {"帮我", "给我", "请你", "我给", "可以", "能不能", "一下", "这样", "这个", "那个"} and not re.fullmatch(
        r"[零一二两三四五六七八九十百千0-9]+[个句段项份次条张]", word)


def _overlaps_source(subject, source):
    words = re.findall(r"[a-z0-9_]{2,}", subject.casefold())
    pairs = re.findall(r"(?=([\u3400-\u9fff]{2}))", subject)
    return any(_scope_word(token) and token in source.casefold() for token in [*words, *pairs])


def _item_coordinate(subject, number, count, message):
    ordinals = {_number(value) for value in _ORDINAL.findall(subject)}
    if ordinals:
        return ordinals == {number}
    if _FIRST.search(subject):
        return number == 1
    if _LAST.search(subject):
        return number == count
    if count == 2 and _OTHER.search(subject):
        other = 3 - number
        return (other in {_number(value) for value in _ORDINAL.findall(message)}
                or bool((_FIRST if other == 1 else _LAST).search(message)))
    return False


def grounded_patch(patch, card, message):
    if not patch or not card:
        return False
    fields = card.get("editable", {})
    source = fields.get(patch.field)
    subject, requirement = patch.subject.strip(), patch.requirement.strip()
    if not source or not subject or not requirement or subject not in message or requirement not in message:
        return False
    if patch.replacement_goal and (patch.field != "goal" or not patch.replacement_goal.strip()
                                   or patch.replacement_goal not in message or requirement not in patch.replacement_goal):
        return False
    # A copied utterance, or two nested copies of it, does not identify a change.
    if subject == requirement or requirement in subject:
        return False
    if patch.field in {"goal", "execution"}:
        return bool(_WORK.search(subject)) or _overlaps_source(subject, source)
    number = int(patch.field.split(":")[1])
    return _overlaps_source(subject, source) or _item_coordinate(subject, number, sum(k.startswith("item:") for k in fields), message)


def patch_vocabulary(cards, message):
    """Bind the subject and the rest of its owner-quoted clause before model selection."""
    choices = {}
    bindings = {}
    for sentence in re.findall(r"[^。！？!?;；\n]+", message):
        clauses = list(re.finditer(r"[^，,、：:]+", sentence))
        mentions = []
        for clause in clauses:
            matches = set()
            for card in cards:
                fields = card.get("editable", {})
                count = sum(key.startswith("item:") for key in fields)
                for field, source in fields.items():
                    text = clause.group()
                    blocks = SequenceMatcher(None, text.casefold(), source.casefold()).get_matching_blocks()
                    words = {text[m.a:m.a + m.size] for m in blocks if m.size >= 2 and _scope_word(text[m.a:m.a + m.size])}
                    positional = field.startswith("item:")
                    patterns = (_ORDINAL, _FIRST, _LAST, _OTHER) if positional else (_WORK,)
                    words.update(m.group() for pattern in patterns for m in pattern.finditer(text))
                    for word in words:
                        if not positional or _overlaps_source(word, source) or _item_coordinate(
                            word, int(field.split(":")[1]), count, message
                        ):
                            matches.add((field, word))
            mentions.append(matches)
        for i, clause in enumerate(clauses):
            for field, word in mentions[i]:
                # A following owner clause about a different item belongs to that item.
                end = next((clauses[j-1].end() for j in range(i+1, len(clauses))
                            if field.startswith("item:") and any(
                                k.startswith("item:") and k != field for k, _ in mentions[j])), len(sentence))
                fragment = sentence[clause.start():end]
                before, _, after = fragment.partition(word)
                following = clauses[i + 1] if i + 1 < len(clauses) else None
                if field == "goal" and following and _GOAL_CHANGE.match(following.group()):
                    # The description before the comma names the existing work.
                    values = {sentence[following.start():end].strip()}
                else:
                    values = {v.strip(" ，,、：:") for v in (before, after)} - {""}
                if values:
                    choices.setdefault((field, word), set()).update(values)
                    owners = bindings.setdefault(field, [])
                    if fragment not in owners:
                        owners.append(fragment)
    return choices, bindings

async def review_change(client, message, card, proposal, choices, bindings):
    """A read-only text review chooses concrete field/quotation pairs. No domain tools."""
    operands = {}
    for (field, subject), values in choices.items():
        operands.setdefault(field, []).extend(TaskPatch(field=field, subject=subject, requirement=v) for v in values)
    keep = "沿用现有内容"
    updates = {field: "现在另选或修改内容" for field in operands if field.startswith("item:")}
    state_schema = {"type": "object", "additionalProperties": False,
                    "properties": {f: {"type": "string", "enum": [keep, action]} for f, action in updates.items()},
                    "required": list(updates)}
    schema = {"type": "object", "additionalProperties": False, "properties": {
        "operation": {"type": "string", "enum": ["answer", "past_reason", "edit_existing", "stop_run", "new_task", "unclear"]},
        "changes": state_schema, "separate_deliverable": {"type": "string"},
        "replacement_goal": {"type": "string", "description": TaskPatch.model_fields["replacement_goal"].description}},
        "required": ["operation", "changes", "separate_deliverable", "replacement_goal"]}
    data = {"owner_message": message, "existing_work": card.get("goal"), "editable": card.get("editable", {}),
            "owner_clauses": bindings, "omni_proposal": proposal}
    reply = await client(input=[{"role": "user", "content": dump(data)}], instructions=(
        "独立核对号主是否现在委托工坊改变业务状态。只聊天、满意、否决、询问未来后果或纯假设都 answer。"
        "只查过去选择的理由才 past_reason。局部保留而另一项要改是 edit_existing，逐项判断当前内容是沿用还是实际调整。"
        + ("现在要求加入条件是 edit_existing；将条件记入任务，不提前执行条件的后果。" if set(operands) == {"goal"}
           else "附带条件的实际委托保留完整条件。") +
        "停止当前执行是 stop_run。new_task 仅限明确交办独立成品，"
        "在 separate_deliverable 逐字摘取独立要求，其余填空串。只读时每一项均沿用现状。"),
        tools=[{"type": "function", "name": "record_review", "description": "记录判断，不执行任何操作。",
                "strict": True, "parameters": schema}], tool_choice="required")
    if len(reply.calls) != 1 or reply.calls[0].name != "record_review":
        raise ValueError("未收到唯一的改口核对回执")
    args = reply.calls[0].arguments
    operation = args.get("operation")
    states = args.get("changes")
    if not isinstance(states, dict) or states.keys() != updates.keys() or any(
        value not in {keep, updates[field]} for field, value in states.items()
    ):
        raise ValueError("改口核对引用了未知选项")
    changed = [field for field, value in states.items() if value != keep]
    if operation in {"edit_existing", "stop_run"}:
        if operation == "stop_run":
            if changed or "execution" not in operands:
                return "clarify", None
            changed = ["execution"]
        elif not changed:
            if "goal" not in operands:
                return "clarify", None
            changed = ["goal"]
        # Every field in the diff has an owner-bound operand. The execution receives the
        # complete original request and the immutable edition; this quote proves the binding.
        witness = max(operands[changed[0]], key=lambda p: (len(p.requirement), len(p.subject)))
        if changed == ["goal"] and operation == "edit_existing":
            witness.replacement_goal = args.get("replacement_goal", "")
        goal_rule = max(operands["goal"], key=lambda p: len(p.requirement)).requirement if "goal" in changed else ""
        # A clause inside a new whole-task condition is not a second, unconditional
        # instruction to replace that item. Preserve its wording inside the condition.
        witness._item_states = {f: state for f, state in states.items() if f.startswith("item:")
                                and not all(clause in goal_rule for clause in bindings[f])}
        return ("revise" if operation == "edit_existing" else "cancel"), witness
    if operation not in {"answer", "past_reason", "new_task", "unclear"}:
        raise ValueError("未知的改口核对结果")
    if changed and operation == "new_task":
        return "clarify", None
    if operation == "new_task":
        goal = args.get("separate_deliverable")
        distinct = isinstance(goal, str) and goal.strip() and goal in message and not any(
            goal in clause for field, clauses in bindings.items() if field.startswith("item:") for clause in clauses)
        return ("new_work" if distinct else "clarify"), None
    return {"past_reason": "recall_reason", "answer": "discuss", "unclear": "clarify"}[operation], None
