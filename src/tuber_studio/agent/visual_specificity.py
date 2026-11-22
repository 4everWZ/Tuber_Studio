"""Check whether the pixels distinguish the particular name used in an edited field."""

from functools import partial
from typing import Literal

from pydantic import Field

from ..core.models import StrictModel
from ..core.store import dump
from .task_delivery import DelegationError


class VisualAlternative(StrictModel):
    term: str = Field(min_length=1, description="从标题或理由逐字摘出需判断的具体称呼。")
    other: str = Field(min_length=1, description="同一处在相同外观下仍有可能的另一种具体解释。")
    discriminator: str = Field(
        min_length=1, description="两者外观相同时，指认原词实际需要看见的排他性构造、材料依据或身份信息。"
    )


class Distinction(StrictModel):
    verdict: Literal["原词仅描述共同可见特征", "画面特征可排除另一解释", "另一解释仍可能或更符合"]
    common_label: str = Field(
        description="对两种解释都准确的一个短称呼，如只描述形状、色泽、图案。单写共有称呼，不能列出两种可能名称。"
    )
    observation: str = Field(
        min_length=1, description="具体指出本图实际可见且两种解释都支持的属性；看不清则说明。"
    )
    distinguishing_cue: str = Field(description="本图中确实可见并能排除另一解释的特征，否则填空。")
    identifying_feature: str = Field(
        description="本帧可定位且正面识别原类别的独有构造、材料结构或身份标记。仅能排除所列对照、或另一种安装方式也能呈现此外观时填空。"
    )


class Inspection(StrictModel):
    x: int = Field(ge=0, le=1000, description="所审物件中心的横坐标；左边缘为0，右边缘为1000。")
    y: int = Field(ge=0, le=1000, description="所审物件中心的纵坐标；顶边为0，底边为1000。")
    claimed_property: str = Field(description="假设两者颜色、轮廓与纹样相同，指认原物仍需核实什么类别特征？")


class Read(StrictModel):
    observation: str = Field(description="主帧的客观画面观察。只写本帧可定位的轮廓、构图、样式、纹样和结构。")


class FrameStructure(StrictModel):
    seen: str = Field(description="具体描述要求核对的位置及可见连接、构造或表面形态；仅据此帧。")
    not_resolved: str = Field(description="查询要求的哪些特征不可见或无法确认？")


class SharedAppearance(StrictModel):
    details: list[str] = Field(
        min_length=1, max_length=4, description="可以单独引用的完整可见事实，每条只描述一个细节。"
    )


class CounterfactualReading(StrictModel):
    counterfactual_clause: str = Field(min_length=1, description="把假定解释写入原句：只把它实际指称的原词换成假定名称，保留其余字词、语序和标点。")
    basis: str = Field(min_length=1, description="解释两个词的包含关系，并核对原句的其它具体用词。")
    original_name: Literal["另一解释本身就是原称呼的一种", "原称呼指画中被表现的形象，另一解释是其载体",
                           "原称呼指定了另一解释不能保证的种类或属性"]
    unsupported_modifiers: list[str] = Field(description="原名之外，原句还有哪些修饰或关联缺少画面依据？逐字摘录，均可见则返回空数组。")

    def replaced_words(self, original, alternative):
        before, sep, after = self.counterfactual_clause.partition(alternative)
        if sep and original.startswith(before) and original.endswith(after):
            return original[len(before):len(original) - len(after)]
        return ""


class NameComparison(StrictModel):
    original_meaning: str = Field(description="原文对这个对象具体断言了什么？")
    alternative_meaning: str = Field(description="对照用词对同一对象具体断言了什么？")
    conflict: bool = Field(description="两种说法是否将同一处指认为不同具体类别、不同材质或互不相容的身份？")


class NameInclusion(StrictModel):
    example: str = Field(description="举出符合对照词、却不符合原称呼的普通实例。不存在则写空。")
    relation: Literal["每个对照实例都属于原称呼", "存在不属于原称呼的对照实例"]


async def _names_conflict(assertion, hypothesis, generate, budget):
    # The alternative reader can paraphrase a whole sentence. Compare what the
    # two names assert without asking the visual reader to select an object again.
    reply = await generate(
        [{"role": "system", "content": "做中文词义辨析。只按所给原句实际使用的词义判断。"
          "两种说法都可用时没有冲突：包括外形比喻与实物、具体名称与它的泛称、形象与载体，以及修饰不同对象。"
          "同一处改称为另一种具体器物、材质或身份，才构成命名冲突。"},
         {"role": "user", "content": dump({"原句": assertion, "原文用词": hypothesis.term, "对照用词": hypothesis.other})}],
        NameComparison.model_json_schema(), max_tokens=budget,
    )
    return NameComparison.model_validate(reply)


async def _read_under_alternative(assertion, hypothesis, proof, image, generate, budget):
    packet = {"整句原文": assertion, "原称呼": hypothesis.term, "假定另一解释为真": hypothesis.other,
              "共有形象": proof.common_label, "原图所见": proof.observation}
    reply = await generate(
        [
            {"role": "system", "content": "做反事实核对。假定镜头内实物确实属于另一解释，外观仍与照片完全相同。"
             "不要判断实物更可能是什么。区分它本身是什么、它表现的是什么形象、它与什么东西外观相似。"
             "先说明原称呼在原句中指实物还是被表现的形象；若指形象，具体说出另一解释所表现的内容。"
             "仅仅同属一个大类或装饰相像不足以把实物定为原称呼。"
             "再逐字核对剩余修饰语，即便主体称呼成立，也要指出无法据原图确认的具体修饰。"},
            {"role": "user", "content": [{"type": "text", "text": dump(packet)}, image]},
        ],
        CounterfactualReading.model_json_schema(),
        max_tokens=budget,
    )
    return CounterfactualReading.model_validate(reply)


async def shared_visual_facts(image, primary, raw_observation, inventory, terms, generate, budget,
                              *, field=None, candidate_reviews=None, reviews=None, frame=None):
    audit = candidate_reviews if candidate_reviews is not None else []
    observations = [*inventory, raw_observation]
    reply = await generate(
        [
            {
                "role": "system",
                "content": "用原图核对同一主帧的观察。原稿对主体的具体称呼有歧义。"
                "从所给观察逐字摘取两三条可独立作标题的短语或句子，优先主体的纹样、轮廓、色彩或位置。"
                "也可选同一画面内另一项明确细节。只选在不同解释下仍可从原图指出的事实。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": dump({"原帧观察": observations, "有争议的称呼": terms})},
                    image,
                ],
            },
        ],
        SharedAppearance.model_json_schema(),
        max_tokens=budget,
    )
    quotes = [
        q
        for q in SharedAppearance.model_validate(reply).details
        if any(q in o for o in observations) and not any(t in q for t in terms)
    ]
    remaining = [q for q in inventory if q not in quotes and not any(t in q for t in terms)]
    for candidates in (quotes, remaining):
        options = {str(i): text for i, text in enumerate(dict.fromkeys(candidates)) if text.strip()}
        if options:
            disputed = await review_visual_specificity(options, image, primary, raw_observation, generate, budget,
                inventory=inventory, reviews=reviews, frame=frame, purpose=f"repair_options:{field}")
            for candidate, row in disputed.items():
                audit.append(row | {"field": field or row["field"], "candidate": options[candidate],
                                    "stage": "review_repair_candidate"})
            supported = [fact for i, fact in options.items() if i not in disputed]
            if supported:
                return supported
    raise DelegationError("主帧复读仍缺少足以确定称呼的画面依据。", attribution_reviews=audit)


async def review_visual_specificity(fields, image, primary, raw_observation, generate, budget, *,
                                    inventory=(), reviews=None, frame=None, purpose="submission"):
    """Test competing readings of a quoted claim against the bound primary image."""
    record = {"purpose": purpose, "fields": dict(fields), "primary_frame": primary, "frame": frame,
              "upstream_observations": {"sampling": raw_observation, "inventory": list(inventory)},
              "stages": [], "object_checks": [], "status": "incomplete"}
    if reviews is not None:
        reviews.append(record)

    async def stage(name, context, messages, schema, *, max_tokens):
        entry = {"stage": name, **context}
        record["stages"].append(entry)
        reply = await generate(messages, schema, max_tokens=max_tokens)
        entry["reply"] = reply
        return reply

    rows = await _review_fields(fields, image, primary, stage, budget, record["object_checks"])
    record.update(status="completed", decisions={
        path: {"disputed": path in rows, "targets": [c["target"] for c in rows.get(path, {}).get("claims", [])],
               "unlocated": rows.get(path, {}).get("unlocated", [])} for path in fields})
    return rows


def _compared_term(hypothesis, prior):
    # A long comparison can change the person while keeping their belongings
    # verbatim on both sides. Only a changed name was actually considered.
    return (hypothesis.term in prior.term and hypothesis.term not in prior.other
            or prior.term in hypothesis.term and prior.term not in hypothesis.other and prior.other in hypothesis.other)


def _alternatives_schema(paths, *, single_object=False):
    choice = VisualAlternative.model_json_schema()
    if single_object:
        choice["properties"]["term"]["description"] = "逐字摘出单个物件或材质名词。含多个对象的短语须拆开。"
    by_field = {"type": "object", "properties": {p: {"type": "array", "items": choice} for p in paths},
                "required": list(paths), "additionalProperties": False}
    return {"type": "object", "properties": {"alternatives": by_field},
            "required": ["alternatives"], "additionalProperties": False}


async def _check_clause_contrasts(alternatives, paths, image, stage, budget):
    opening = await stage("read_frame", {}, [
        {"role": "system", "content": "独立查看这张截图。按前景、主体、背景描述直接可见的细节。材质、具体用途和人物身份只有明确标记才可指认。"},
        {"role": "user", "content": [image]}], Read.model_json_schema(), max_tokens=budget)
    Read.model_validate(opening)
    cases = {str(i): h.model_dump() for i, h in enumerate(alternatives)}
    schema = {
        "type": "object",
        "properties": {i: Distinction.model_json_schema() for i in cases},
        "required": list(cases),
        "additionalProperties": False,
    }
    checked = await stage("clause_pixels", {"cases": cases, "paths": [sorted(p) for p in paths]},
        [
            {
                "role": "system",
                "content": "只看原图，逐项比较这两种解释。相同纹样、色泽和轮廓不能区分"
                "相同外观下的用途、材质或身份。只有能在本帧指出排他性特征才能确定具体名称。原词本身已是两种解释的"
                "共有描述时判共同特征。初读的名词也只是候选，最终依据必须能在图中指出。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": dump({"待辨别": cases, "主帧初读": opening["observation"]})},
                    image,
                ],
            },
        ],
        schema,
        max_tokens=budget,
    )
    if set(checked) != set(cases):
        raise DelegationError("主帧具体称呼的辨别依据未逐项返回。")
    return checked


async def _check_additional_objects(alternatives, paths, clause_count, assertions, image, stage, budget):
    checked = {}
    local_schema = Distinction.model_json_schema()
    local_schema["properties"]["verdict"] = local_schema["properties"].pop("verdict")
    for index in range(clause_count, len(alternatives)):
        key = str(index)
        case = alternatives[index].model_dump()
        path = sorted(paths[index])[0]
        inspection = Inspection.model_validate(await stage("inspection", {"case": key, "field": path}, [
            {"role": "system", "content": "在原图定位被审物件。另说明此名词本身比相同外观的另一解释多承诺哪项性质。"
             "物件所属类别与它的外观修饰分别考虑。问句用来指认对象的词仍须核实。"},
            {"role": "user", "content": [{"type": "text", "text": dump({"原句": assertions[path], "待辨别": case})}, image]}
        ], Inspection.model_json_schema(), max_tokens=budget))
        # The independent reader only gets coordinates. Free-form locator
        # questions can contain the disputed noun and presuppose its identity.
        query = {"横坐标": inspection.x, "纵坐标": inspection.y}
        structure = await stage("read_frame", {"case": key, "query": query}, [
            {"role": "system", "content": "坐标范围为0至1000，原点是左上角。只看原图坐标处的物件，记录其可见轮廓、部件、"
             "表面、与周围物体的连接关系。指出哪些构造无法辨明。不要根据环境推测用途、身份或材质。"},
            {"role": "user", "content": [{"type": "text", "text": dump(query)}, image]}
        ], FrameStructure.model_json_schema(), max_tokens=budget)
        observed = FrameStructure.model_validate(structure).model_dump()
        schema = {"type": "object", "properties": {key: local_schema},
                  "required": [key], "additionalProperties": False}
        decision = await stage("compare_pixels", {"case": key, "paths": sorted(paths[index]), "hypothesis": case}, [
            {"role": "system", "content": "按独立局部读图结果审查原词。单凭形似、其它物体或观察中的同名词不能确认特定类型。"
             "若原词承诺额外性质、但记录不能正面识别，判另一解释仍可能。"
             "若原词在句中仅指共同外观、以形状作比喻、风格或被表现的形象，则判共同特征。"},
            {"role": "user", "content": dump({"原词": case["term"], "原词额外指定的性质": inspection.claimed_property,
                                            "对照": case["other"], "独立结构记录": observed})}
        ], schema, max_tokens=budget)
        if set(decision) != {key}:
            raise DelegationError("主帧具体称呼的辨别依据未逐项返回。")
        checked.update(decision)
    return checked


async def _review_fields(fields, image, primary, stage, budget, object_checks):
    assertions = {}
    for path, text in fields.items():
        detail, sep, _ = text.partition("。剪辑安排：")
        assertions[path] = (
            detail.removeprefix("画面依据：")
            if path.endswith(".rationale") and sep and detail.startswith("画面依据：")
            else text
        )
    schema = _alternatives_schema(fields)
    reply = await stage("clause_contrasts", {"assertions": assertions},
        [
            {
                "role": "system",
                "content": "仅阅读待审文字，分析稿件中对器物种类、材质及人物身份的断言。"
                "找出有另一种合理解释的具体称呼，包括标题和理由共同采用的名词。对同一处提出能解释所见形状、"
                "却具有不同构造、材质、用途或身份的候选。两种解释都保留原句中的颜色、纹样和装饰复杂程度；"
                "只质疑正文实际认定的具体类别；形似、并列的可能解释或画面上逐字引用的内容并非对实物类型的认定。"
                "另一解释必须与原文主张不相容，不能用原词的上位概念与它对立。只摘录最短的具体称呼，说明需要什么特征才能排除它。"
                "原始取材观察也可能猜错。按各字段实际断言分别填写；一般位置、颜色、仅描述形似或两种候选共有的描述填空数组。",
            },
            {"role": "user", "content": dump(assertions)},
        ],
        schema,
        max_tokens=budget,
    )
    alternatives = []
    paths = []
    if set(reply["alternatives"]) != set(fields):
        raise DelegationError("具体称呼审查未覆盖待审字段。")
    for path, entries in reply["alternatives"].items():
        for entry in entries:
            hypothesis = VisualAlternative.model_validate(entry)
            if hypothesis.term != hypothesis.other and hypothesis.term in assertions[path]:
                if hypothesis not in alternatives:
                    alternatives.append(hypothesis)
                    paths.append(set())
                paths[alternatives.index(hypothesis)].update(
                    p for p, assertion in assertions.items() if assertion == assertions[path]
                )
    clause_count = len(alternatives)
    object_names = {path: set() for path in fields}
    for assertion in dict.fromkeys(assertions.values()):
        group = [p for p, text in assertions.items() if text == assertion]
        path = group[0]
        schema = _alternatives_schema([path], single_object=True)
        reply = await stage("nominate", {"assertion": assertion, "fields": group}, [
            {"role": "system", "content": "逐字抽取稿件里的实体称呼。逐个检查穿戴物、手持物、布景及附属物，"
             "包含陈述和问题预设的类别、材料、特定身份。每个需要特别构造、用途或材料才成立的物名各占一项。"
             "先摘最短原词；再写其余可见外形完全相同、但缺少该定义属性的另一解释；"
             "最后写出证明原词的可观察条件。不能取同类的另一款作反例。"
             "画中人物或图案只指画上呈现的形象，不要求证明画外实物真伪。仅描述颜色、形似、宽泛类别的词可无异议。"},
            {"role": "user", "content": dump({path: assertion})}], schema, max_tokens=budget)
        if set(reply["alternatives"]) != {path}:
            raise DelegationError("具体称呼审查未覆盖待审字段。")
        for entry in reply["alternatives"][path]:
            h = VisualAlternative.model_validate(entry)
            binding = {"fields": group, "hypothesis": entry}
            object_checks.append(binding)
            if h.term == h.other or h.term not in assertion:
                binding["status"] = "no_bound_contrast"
                continue
            for p in group:
                object_names[p].add(h.term)
            covered = {p: [i for i, prior in enumerate(alternatives[:clause_count])
                           if p in paths[i] and _compared_term(h, prior)] for p in group}
            if all(covered.values()):
                binding.update(status="covered_by_clause", cases=covered)
                continue
            # A clause-level entry with the same wording might have left the
            # object unchanged on both sides. Give it its own local inspection.
            extra = alternatives[clause_count:]
            if h not in extra:
                alternatives.append(h)
                paths.append(set())
                index = len(alternatives) - 1
            else:
                index = clause_count + extra.index(h)
            paths[index].update(group)
            binding.update(status="local_inspection", case=index)
    if not alternatives:
        return {}
    clauses = await _check_clause_contrasts(alternatives[:clause_count], paths[:clause_count], image, stage, budget) if clause_count else {}
    local = await _check_additional_objects(alternatives, paths, clause_count, assertions, image, stage, budget)
    checked = clauses | local
    receipts = {}
    substitutions = {}
    objections = []
    for i, h in enumerate(alternatives):
        proof = Distinction.model_validate(checked[str(i)])
        if (
            proof.verdict == "画面特征可排除另一解释"
            and proof.distinguishing_cue.strip()
            and proof.identifying_feature.strip()
        ):
            continue
        claim = (
            h.model_dump()
            | proof.model_dump()
            | {
                "target": h.term,
                "phrase": h.term,
                "relation": "画面具体称呼",
                "issue": "画面审读未给出一致且能区分具体称呼的依据。可采用共有的可见属性或其它已核细节。",
            }
        )
        affected = set(paths[i])
        # A decorated title can quote the complete visual clause of its rationale. If both
        # fields assert the same name there, an objection to it applies to both occurrences.
        for j, peer in enumerate(alternatives):
            if h.term not in peer.term and peer.term not in h.term:
                continue
            for path in paths[j]:
                if h.term in assertions[path] and any(
                    assertions[p] in assertions[path] or assertions[path] in assertions[p] for p in paths[i]
                ):
                    affected.add(path)
        readings = {}
        comparisons = {}
        for path in sorted(affected):
            field_claim = claim
            targets = [h.term]
            if proof.verdict == "原词仅描述共同可见特征":
                assertion = assertions[path]
                if assertion not in readings:
                    readings[assertion] = await _read_under_alternative(assertion, h, proof, image,
                        partial(stage, "counterfactual", {"case": i, "field": path}), budget)
                reading = readings[assertion]
                replaced = reading.replaced_words(assertion, h.other)
                if replaced:
                    substitutions.setdefault((assertion, h.other), set()).add(replaced)
                targets = [t for t in reading.unsupported_modifiers if t.strip() and t in assertion]
                field_claim = claim | {"counterfactual": reading.model_dump(), "issue": reading.basis}
                if reading.original_name == "原称呼指定了另一解释不能保证的种类或属性":
                    targets.append(h.term)
                elif (i >= clause_count and replaced == h.term
                      and reading.original_name == "另一解释本身就是原称呼的一种"):
                    # A grammatical replacement in a question says nothing
                    # about whether every alternative is actually that object.
                    inclusion = NameInclusion.model_validate(await stage("name_inclusion", {"case": i, "field": path}, [
                        {"role": "system", "content": "只辨析两个名词的外延。对照词所指的每个实例都是原称呼的一种吗？"
                         "只要有同类反例，便不能把对照实例无条件称为原词。"},
                        {"role": "user", "content": dump({"对照词": h.other, "原称呼": h.term})}
                    ], NameInclusion.model_json_schema(), max_tokens=budget))
                    if inclusion.relation == "存在不属于原称呼的对照实例":
                        targets.append(h.term)
                        field_claim = field_claim | {"name_inclusion": inclusion.model_dump()}
                elif (
                    not any(h.term in text for text in (proof.common_label, proof.observation, reading.counterfactual_clause))
                    or (assertion != h.term and not replaced and assertion not in reading.counterfactual_clause)
                ):
                    if assertion not in comparisons:
                        comparisons[assertion] = await _names_conflict(assertion, h,
                            partial(stage, "compare_names", {"case": i, "field": path}), budget)
                    comparison = comparisons[assertion]
                    if comparison.conflict:
                        targets.append(h.term)
                        field_claim = field_claim | {"name_comparison": comparison.model_dump()}
                missing = [term for term in reading.unsupported_modifiers if not term.strip() or term not in assertion]
                if missing:
                    row = receipts.setdefault(path, {"field": path, "text": fields[path], "primary_frame": primary,
                                                      "claims": [], "pages": []})
                    row.setdefault("unlocated", []).append({"modifiers": missing, "hypothesis": h.model_dump(),
                        "basis": reading.basis, "counterfactual": reading.model_dump()})
                if not targets:
                    continue
            objections.append((path, h, field_claim, targets))
    for path, h, claim, targets in objections:
        slots = substitutions.get((assertions[path], h.other), set())
        if len(slots) == 1:
            slot = next(iter(slots))
            if h.term not in slot and slot not in h.term:
                # The alternative belongs to a different name. Independently located
                # modifiers in this field still need correction.
                targets = [target for target in targets if target != h.term]
                if not targets:
                    continue
        row = receipts.setdefault(path, {"field": path, "text": fields[path], "primary_frame": primary,
                                         "claims": [], "pages": []})
        for target in dict.fromkeys(targets):
            if target == h.term:
                # Narrow only the name actually replaced by this contrast. Keep
                # independently disputed modifiers, even inside the same phrase.
                for name in sorted(object_names[path], key=lambda n: (len(n), n)):
                    before, found, after = h.term.partition(name)
                    if found and name not in h.other and h.other.startswith(before) and h.other.endswith(after):
                        target = name
                        break
            objection = claim | {"term": target, "target": target, "phrase": target}
            if target == h.term and (comparison := claim.get("name_comparison")):
                objection["issue"] = f"原词指{comparison['original_meaning']}；同一外观下也可能是{comparison['alternative_meaning']}。"
            row["claims"].append(objection)
    return receipts
