"""Check whether the pixels distinguish the particular name used in an edited field."""

from typing import Literal

from pydantic import Field

from ..models import StrictModel
from ..store import dump
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


async def shared_visual_facts(image, primary, raw_observation, inventory, terms, generate, budget):
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
            disputed = await review_visual_specificity(options, image, primary, raw_observation, generate, budget)
            supported = [fact for i, fact in options.items() if i not in disputed]
            if supported:
                return supported
    raise DelegationError("主帧复读仍缺少足以确定称呼的画面依据。")


async def review_visual_specificity(fields, image, primary, raw_observation, generate, budget):
    """Test competing readings of a quoted claim against the bound primary image."""
    assertions = {}
    for path, text in fields.items():
        detail, sep, _ = text.partition("。剪辑安排：")
        assertions[path] = (
            detail.removeprefix("画面依据：")
            if path.endswith(".rationale") and sep and detail.startswith("画面依据：")
            else text
        )
    by_field = {
        "type": "object",
        "properties": {p: {"type": "array", "items": VisualAlternative.model_json_schema()} for p in fields},
        "required": list(fields),
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {"alternatives": by_field},
        "required": ["alternatives"],
        "additionalProperties": False,
    }
    reply = await generate(
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
    if not alternatives:
        return {}
    cases = {str(i): h.model_dump() for i, h in enumerate(alternatives)}
    schema = {
        "type": "object",
        "properties": {i: Distinction.model_json_schema() for i in cases},
        "required": list(cases),
        "additionalProperties": False,
    }
    checked = await generate(
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
                    {"type": "text", "text": dump({"待辨别": cases, "主帧初读": raw_observation})},
                    image,
                ],
            },
        ],
        schema,
        max_tokens=budget,
    )
    if set(checked) != set(cases):
        raise DelegationError("主帧具体称呼的辨别依据未逐项返回。")
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
        for path in sorted(affected):
            field_claim = claim
            targets = [h.term]
            if proof.verdict == "原词仅描述共同可见特征":
                assertion = assertions[path]
                if assertion not in readings:
                    readings[assertion] = await _read_under_alternative(assertion, h, proof, image, generate, budget)
                reading = readings[assertion]
                replaced = reading.replaced_words(assertion, h.other)
                if replaced:
                    substitutions.setdefault((assertion, h.other), set()).add(replaced)
                targets = [t for t in reading.unsupported_modifiers if t.strip() and t in assertion]
                if (reading.original_name == "原称呼指定了另一解释不能保证的种类或属性"
                        or not any(h.term in text for text in (proof.common_label, proof.observation, reading.counterfactual_clause))):
                    targets.append(h.term)
                if not targets and reading.unsupported_modifiers:
                    # The reading raised an objection but its wording was not located.
                    targets.append(assertion)
                if not targets and assertion != h.term and not replaced and assertion not in reading.counterfactual_clause:
                    # Other words changed too; the proposed reading did not verify the complete clause.
                    targets.append(assertion)
                if not targets:
                    continue
                field_claim = claim | {"counterfactual": reading.model_dump(), "issue": reading.basis}
            objections.append((path, h, field_claim, targets))
    for path, h, claim, targets in objections:
        slots = substitutions.get((assertions[path], h.other), set())
        if len(slots) == 1:
            slot = next(iter(slots))
            if h.term not in slot and slot not in h.term:
                # A faithful substitution binds this same alternative to another part of the same clause.
                continue
        row = receipts.setdefault(path, {"field": path, "text": fields[path], "primary_frame": primary,
                                         "claims": [], "pages": []})
        for target in dict.fromkeys(targets):
            row["claims"].append(claim | {"term": target, "target": target, "phrase": target})
    return receipts
