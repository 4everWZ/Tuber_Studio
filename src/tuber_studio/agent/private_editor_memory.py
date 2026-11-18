"""Owner-only writing preferences for the final, tool-free Omni editor."""

from pydantic import Field

from ..daily_memory import MemoryChanged
from ..generation_meter import budgeted_call
from ..models import StrictModel
from ..store import dump
from .owner_memory import MemoryUse, check_use, citation, correctable_preference, private_rows, read_stamp
from .editor_contract import FormatCheck, literal_checks, review_preferences
from .task_delivery import DelegationError


class WritingSelection(StrictModel):
    applies: bool = Field(strict=True)
    checks: list[FormatCheck] = Field(max_length=12)


def connect_private_editor(runtime):
    tasks = runtime.tasks
    editor = tasks.edit_delivery

    async def edit(task, evidence):
        plan = task["plan"]
        selected_video = bool(plan.get("video_selection")) or any(step["tool"] == "inspect_video" for step in task["steps"])
        attachment = any(step["tool"] == "owner_attachment" for step in task["steps"])
        if not (selected_video or attachment):
            return await editor(task, evidence)
        if any(step["tool"].endswith("_grant") for step in task["steps"]):
            return await editor(task, evidence)
        scope = runtime.memory_organizer.scope()
        memory = runtime.daily_memory
        if not memory.private_owner or not memory.preference(scope)["enabled"]:
            return await editor(task, evidence)
        private_plan = plan.copy()
        inherited = {}
        previous = plan.get("private_previous_edition")
        if previous:
            previous_use = MemoryUse.model_validate(previous["memory_use"])
            try:
                check_use(runtime, previous_use)
            except MemoryChanged:
                pass
            else:
                private_plan.update(previous["fields"])
                inherited = {identity: memory.organized(scope, item_id=identity)[0] for identity in previous_use.ids}
        request = task | {"plan": private_plan}
        # Scope and provenance limit access before a private model selects by editorial purpose.
        matches = private_rows(runtime, task["goal"])
        recent = memory.organized(scope, limit=48)
        candidates = dict(inherited)
        for row in [*matches, *recent]:
            if correctable_preference(row, memory.private_owner):
                if len(candidates) < 8:
                    candidates.setdefault(row["id"], row)
            if len(candidates) == 8:
                break
        if not candidates:
            return await editor(request, evidence)
        candidates = list(candidates.values())
        selection_use = MemoryUse(ids=[r["id"] for r in candidates], stamp=read_stamp(runtime, candidates))
        instruction = ("判断这条号主偏好是否适用于本次私有成果的组织、措辞或选材。"
                     "排除个人事实、身份、无关偏好和行动授权。本轮明确覆盖的约定不选。"
                     "将原话明确要求每个字段遵守的字面前缀、后缀、包含文字或字符数界限列入 checks。"
                     "value 填实际字符（标点名转为对应符号）或十进制数。"
                     "局部条件或语义要求交由成品复核，不推测阈值。无字面约束时 checks 为空。")
        schema = WritingSelection.model_json_schema()
        async def generate(messages, schema, use):
            async with runtime.operator_generation("background"):
                check_use(runtime, use)
                provider = runtime.llm
                limit = min(runtime.settings.data["llm"]["max_tokens"], 1024)
                result = await budgeted_call(provider, lambda: provider.generate_json(messages, schema, max_tokens=limit),
                                             messages, max_tokens=limit)
                check_use(runtime, use)
                return result
        notes, checks = {}, []
        for row in candidates:
            messages = [{"role":"system","content":instruction},
                        {"role":"user","content":dump({"goal":task["goal"], "writing_preference":row["anecdote"]})}]
            selection = WritingSelection.model_validate(await generate(messages, schema, selection_use))
            if selection.applies:
                notes[row["id"]] = row["anecdote"]
                bound_checks = literal_checks(row["anecdote"])
                bound_checks.extend(check for check in selection.checks if check not in bound_checks)
                checks.extend((row["id"], check) for check in bound_checks)
        rows = [r for r in candidates if r["id"] in notes or r["id"] in inherited]
        if not rows:
            return await editor(request, evidence)
        use = MemoryUse(ids=[row["id"] for row in rows], stamp=read_stamp(runtime, rows))
        plan["editor_memory"] = {"memory_use": use.model_dump(), "memory_citations": [citation(r) for r in rows]}
        try:
            request = request | {"owner_writing_preferences": list(notes.values())}
            product = await editor(request, evidence)
            check_use(runtime, use)
            if not notes:
                return product
            locked = getattr(product, "_locked_fields", set())
            async def verify(draft):
                return await review_preferences(task["goal"], notes, checks, draft,
                                                lambda messages,schema: generate(messages,schema,use), locked)
            if issues := await verify(product):
                instructions = {path:"保留事实、证据和选材，按号主约定改写：" + "；".join(notes[k] for k in ids)
                                for path,ids in issues.items()}
                repair = private_plan | {"editor_draft":product.model_dump(), "editor_issues":instructions,
                                         "frame_reviews":product._frame_reviews,
                                         "window_reviews":getattr(product, "_window_reviews", {})}
                product = await editor(request | {"plan":repair}, evidence)
                check_use(runtime, use)
                if remaining := await verify(product):
                    diagnostics = "；".join(f"{path}（约定 " + "、".join(f"#{k}" for k in ids) + "）"
                                           for path,ids in remaining.items())
                    raise DelegationError("成品仍未满足本轮写作约定：" + diagnostics + "。尚未交付。")
            plan["editor_memory"]["review"] = {"preference_ids":list(notes), "format_checks":len(checks),
                                               "repaired_fields":sorted(issues)}
            return product
        finally:
            check_use(runtime, use)

    tasks.edit_delivery = edit
