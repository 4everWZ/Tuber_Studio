"""Owner-only writing preferences for the final, tool-free Omni editor."""

from pydantic import Field

from ..cooperation import collaboration_text
from ..daily_memory import MemoryChanged
from ..generation_meter import budgeted_call
from ..models import StrictModel
from ..store import dump
from .editor_contract import FormatCheck, literal_evidence, review_preferences
from .owner_memory import MemoryUse, check_use, citation, correctable_preference, private_rows, read_stamp
from .task_delivery import DelegationError
from .title_form import (
    RequestedTitleForm,
    explicit_title_form,
    form_instruction,
    frame_observations,
    grounded_options,
    meets_form,
    read_title,
)


class WritingSelection(StrictModel):
    applies: bool = Field(strict=True)
    checks: list[FormatCheck] = Field(max_length=12)
    title_form: RequestedTitleForm | None = None


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
            if snapshot := plan.get("editor_memory"):
                check_use(runtime, MemoryUse.model_validate(snapshot["memory_use"]))
            return await editor(task, evidence)

        def qualified_snapshot(snapshot):
            use = MemoryUse.model_validate(snapshot["memory_use"])
            check_use(runtime, use)
            rows = [memory.organized(scope, item_id=i)[0] for i in use.ids]
            if not all(correctable_preference(row, memory.private_owner) and collaboration_text(row) for row in rows):
                return {}
            return {row["id"]: row for row in rows}

        if plan.get("editor_memory") and not qualified_snapshot(plan["editor_memory"]):
            for key in ("editor_memory", "editor_draft", "editor_issues", "retained_candidates"):
                plan.pop(key, None)
        private_plan = plan.copy()
        inherited = {}
        previous = private_plan.pop("private_previous_edition", None)
        if previous:
            try:
                inherited = qualified_snapshot(previous)
            except MemoryChanged:
                pass
            if inherited:
                private_plan.update(previous["fields"])
        request = task | {"plan": private_plan}
        # Scope and provenance limit access before a private model selects by editorial purpose.
        matches = private_rows(runtime, task["goal"])
        recent = memory.organized(scope, limit=48)
        candidates = dict(inherited)
        for row in [*matches, *recent]:
            if correctable_preference(row, memory.private_owner) and collaboration_text(row):
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
                     "若号主要求每项标题使用特定句式，填 title_form，否则填 null。标点本身不指定句式。"
                     "局部条件或其他语义要求交由成品复核，不推测阈值。无字面约束时 checks 为空。")
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
        notes, checks, forms = {}, [], {}
        format_rules, discarded = [], []
        for row in candidates:
            edited = row["proof"].get("owner_edit_at", 0) > row["source"]["at"]
            owner_text = collaboration_text(row)
            messages = [{"role":"system","content":instruction},
                        {"role":"user","content":dump({"goal":task["goal"], "writing_preference":owner_text,
                              "owner_quote":owner_text})}]
            selection = WritingSelection.model_validate(await generate(messages, schema, selection_use))
            if selection.applies:
                notes[row["id"]] = owner_text
                if form := explicit_title_form(owner_text) or selection.title_form:
                    forms[row["id"]] = form
                bindings = literal_evidence(owner_text)
                source = {"id":row["id"], "revision":row["revision"],
                          "source":"owner_edit" if edited else row["source"]["key"]}
                for binding in bindings:
                    check = binding["check"]
                    if (row["id"], check) not in checks:
                        checks.append((row["id"], check))
                    format_rules.append(source | binding | {"check":check.model_dump()})
                for proposed in selection.checks:
                    if not any(b["check"] == proposed for b in bindings):
                        discarded.append(source | {"suggested":proposed.model_dump(), "reason":"号主原文没有可核对的字段、操作与值跨度"})
        rows = [r for r in candidates if r["id"] in notes or r["id"] in inherited]
        if not rows:
            return await editor(request, evidence)
        use = MemoryUse(ids=[row["id"] for row in rows], stamp=read_stamp(runtime, rows))
        plan["editor_memory"] = {"memory_use": use.model_dump(), "memory_citations": [citation(r) for r in rows]}
        audit = {"preference_ids":list(notes), "format_checks":len(checks), "format_rules":format_rules,
                 "discarded_format_suggestions":discarded, "attempts":[], "alternatives":{}}
        plan["editor_memory"]["review"] = audit
        try:
            directions = [{"owner_preference":note, "title_form":forms[k], "editor_guidance":form_instruction(forms[k])}
                          if k in forms else {"owner_preference":note} for k,note in notes.items()]
            request = request | {"owner_writing_preferences": directions}
            product = await editor(request, evidence)
            check_use(runtime, use)
            if not notes:
                return product
            locked = getattr(product, "_locked_fields", set())
            async def call(messages, schema):
                return await generate(messages, schema, use)

            async def verify(draft):
                locked = getattr(draft, "_locked_fields", set())
                readings = {}
                attempt = {"titles":readings, "submitted_titles":[item.title for item in draft.items]}
                audit["attempts"].append(attempt)
                issues = await review_preferences(task["goal"], notes, checks, draft, call, locked)
                attempt["failed_fields"] = issues
                if forms:
                    for index, item in enumerate(draft.items):
                        path = f"items.{index}.title"
                        if path in locked:
                            continue
                        frame = item.primary_frame
                        observations = frame_observations(draft._frame_reviews.get(frame, {}))
                        if not observations and frame and frame in evidence.observations:
                            observations = [evidence.observations[frame]]
                        reading = await read_title(item.title, observations, call)
                        readings[path] = reading | {"title":item.title, "observations":observations, "expected":forms,
                            "writing_review_issues":list(issues.get(path, [])),
                            "literal_checks_passed":all(c.meets(item.title) for _,c in checks if c.field == "title")}
                        for identity, expected in forms.items():
                            if not meets_form(reading, expected, require_grounding=bool(frame)):
                                ids = issues.setdefault(path, [])
                                if identity not in ids:
                                    ids.append(identity)
                return issues, readings
            issues, readings = await verify(product)
            if issues:
                verified_titles = {}
                instructions = {path:"保留事实、证据和选材，按号主约定改写：" + "；".join(
                                    notes[k] + (form_instruction(forms[k]) if k in forms and path.endswith(".title") else "")
                                    for k in ids)
                                for path,ids in issues.items()}
                for path, reading in readings.items():
                    if path in instructions:
                        instructions[path] += "。本稿去标点后判为" + reading["form"] + "。本字段可用的原始观察：" + dump(reading["observations"])
                        item = product.items[int(path.split(".")[1])]
                        details = product._frame_reviews.get(item.primary_frame, {}).get("details", [])
                        expected = {forms[k] for k in issues[path] if k in forms}
                        if details and len(expected) == 1:
                            rules = [c for _,c in checks if c.field == "title"]
                            options = await grounded_options(details, expected.pop(), rules, call)
                            audit["alternatives"][path] = options
                            if options["chosen"]:
                                verified_titles[path] = {"primary_frame":item.primary_frame, "original":item.title, "fact":options["fact"],
                                                         "title":options["chosen"]}
                repair = private_plan | {"editor_draft":product.model_dump(), "editor_issues":instructions,
                                         "frame_reviews":product._frame_reviews,
                                         "window_reviews":getattr(product, "_window_reviews", {})}
                check_use(runtime, use)
                product = await editor(request | {"plan":repair, "verified_title_edits":verified_titles,
                                                  "editor_locked_fields":sorted(locked)}, evidence)
                check_use(runtime, use)
                remaining, readings = await verify(product)
                if remaining:
                    diagnostics = "；".join(f"{path}（约定 " + "、".join(f"#{k}" for k in ids) + "）"
                                           for path,ids in remaining.items())
                    raise DelegationError("成品仍未满足本轮写作约定：" + diagnostics + "。尚未交付。")
            audit.update(repaired_fields=sorted(issues), title_readings=readings)
            return product
        finally:
            check_use(runtime, use)

    tasks.edit_delivery = edit
