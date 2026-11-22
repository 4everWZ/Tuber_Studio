"""Check the selected owner's presentation requirements on the actual private deliverable."""

import re
from typing import Literal
from unicodedata import normalize

from pydantic import Field, ValidationError

from ..core.identity import without_model_identity
from ..core.models import StrictModel
from ..core.store import dump
from ..infra.inference.http_api import InferenceBudgetExceeded
from .task_delivery import DelegationError, _text, editorial_fields, primary_frame

_PUNCTUATION = {"问号":"？", "句号":"。", "感叹号":"！", "叹号":"！", "冒号":"：", "逗号":"，",
                "分号":"；", "顿号":"、", "省略号":"……", "破折号":"——"}
_FIELD = {"标题":"title", "理由":"rationale", "缺口":"gap"}
_LITERAL = r'(?:「([^」]+)」|“([^”]+)”|"([^"]+)"|(' + "|".join(_PUNCTUATION) + r'|[？?。.!！：:，,；;、]))'
_BOUNDARY = re.compile(r"(?:以|用)" + _LITERAL + r"(?:作为)?(开头|前缀|结尾|收尾|后缀)")
_CONTAINS = re.compile(r"(?:须|必须|要)?(?:包含|含有)" + _LITERAL)
_LENGTH = re.compile(r"(最多|不超过|至少|不少于)\s*([1-9]\d*)\s*(?:个)?(?:字|字符)")


_QUOTED = r'「[^」]*」|“[^”]*”|"[^"]*"'
_SPLIT = re.compile(_QUOTED + r"|[，,。！？；;\n]")
_MODIFIERS = re.compile(r"(?:都|均|一律|统一|全部|须|需|必须|请|应当|应该|应|一定|要|文字|字数|长度|的|格式|\s)*")
_CONDITIONAL = re.compile(r"若|如果|除非|仅当|只有|有时|偶尔|尽量|可以|可选|第[一二三四五六七八九十\d]+|否则|不要|不必|不得|禁止|不能|不以|不用|不要求|不需要|是否|能否|或者|或")
_FIELD_NAME = re.compile("|".join(_FIELD))


def spans(text, delimiters):
    start = 0
    for match in _SPLIT.finditer(text):
        if match[0] in delimiters and not (match.start() and text[match.start()-1] in "以用"
                and re.match(r"(?:作为)?(?:结尾|开头|收尾|前缀|后缀)", text[match.end():])):
            yield start, match.start()
            start = match.end()
    yield start, len(text)


def literal_evidence(note):
    """Bind each executable check to a named field and a directive in the owner's own text."""
    bindings = []
    for begin, end in spans(note, "。！？；;\n"):
        sentence = note[begin:end]
        if _CONDITIONAL.search(re.sub(_QUOTED, "", sentence)):
            continue
        field = None
        for offset, stop in spans(sentence, "，,"):
            clause = sentence[offset:stop]
            bare = re.sub(_QUOTED, lambda m: " " * len(m[0]), clause)
            named = list(_FIELD_NAME.finditer(bare))
            if named:
                field = _FIELD[named[0][0]] if len(named) == 1 else None
                field_start = begin + offset + named[0].start()
            if not field:
                continue
            for pattern in (_BOUNDARY, _CONTAINS, _LENGTH):
                for match in pattern.finditer(clause):
                    prefix = clause[named[0].end() if named else 0:match.start()]
                    if not _MODIFIERS.fullmatch(prefix) or (named and match.start() < named[0].end()):
                        continue
                    if pattern is _LENGTH:
                        op = "max_chars" if match[1] in {"最多","不超过"} else "min_chars"
                        value = match[2]
                    else:
                        value = next(v for v in match.groups()[:4] if v is not None)
                        value = _PUNCTUATION.get(value, value) if match[4] else value
                        op = "contains" if pattern is _CONTAINS else (
                            "starts_with" if match[5] in {"开头","前缀"} else "ends_with")
                    finish = begin + offset + match.end()
                    bindings.append({"check":FormatCheck(field=field, operation=op, value=value),
                                     "start":field_start, "end":finish, "quote":note[field_start:finish]})
    return bindings


def literal_checks(note):
    return [binding["check"] for binding in literal_evidence(note)]


class FormatCheck(StrictModel):
    field: Literal["title", "rationale", "gap"]
    operation: Literal["starts_with", "ends_with", "contains", "max_chars", "min_chars"]
    value: str = Field(min_length=1, max_length=150)

    def meets(self, text):
        length = len(text.strip())
        text = normalize("NFKC", text.strip())
        value = normalize("NFKC", self.value)
        if self.operation == "starts_with":
            return text.startswith(value)
        if self.operation == "ends_with":
            return text.endswith(value)
        if self.operation == "contains":
            return value in text
        if not value.isascii() or not value.isdigit():
            raise DelegationError("写作长度约定缺少数值，尚未交付。")
        return length <= int(value) if self.operation == "max_chars" else length >= int(value)


_SUBJECT = re.compile(r"(?:每(?:个|项|条)|所有|各(?:个|项)?|全部)?(标题|理由|缺口)")
_CONTEXT = re.compile(r"(?:以后|今后)?(?:写|撰写|制作|整理)[^，,。；;：:]{1,30}(?:提纲|稿件|稿|清单|提案|报告)(?:时)?")
_FORM = re.compile(r"(?:使用|采用|写成|改成|改为|用)?(问句|疑问句|陈述句|名词短语)")
_GENRE = re.compile(r"(?:以后|今后)?(?:写|撰写|制作|整理)(?:直播|视频|节目|素材|工作)*(?:提纲|稿件|稿|清单|提案|报告)时?")
_WORD_LENGTH = re.compile(r"(?:最多|不超过|至少|不少于)[一二三四五六七八九十百]+个?(?:字|字符)")


def scoped_fields(note):
    """Only narrow standalone field directives; keep unbound clauses in a whole-document review."""
    named = set()
    for begin, end in spans(note, "，,。！？；;\n"):
        clause = note[begin:end].strip()
        if not clause or (not named and _CONTEXT.fullmatch(clause)):
            continue
        if match := _SUBJECT.match(clause):
            named.add(_FIELD[match[1]])
            clause = clause[match.end():]
        directive = clause[_MODIFIERS.match(clause).end():]
        if len(named) != 1 or not any(pattern.fullmatch(directive) for pattern in
                                     (_FORM, _BOUNDARY, _CONTAINS, _LENGTH, _WORD_LENGTH)):
            return None
    return named or None


class ClauseVerdict(StrictModel):
    clause: str
    verdict: Literal["meets", "revise_fields", "layout_conflict", "unknown"]
    fields: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class WritingJudgment(StrictModel):
    decisions: list[ClauseVerdict] = Field(min_length=1)


class JudgmentCheck(StrictModel):
    supported: bool = Field(strict=True)
    reason: str = Field(min_length=1)


class LayoutMismatch(DelegationError):
    """The rendered blocks contradict the submitted fields or their bound receipts."""


def writing_layout(product, markdown):
    """Bind authored fields to their rendered blocks. Receipt contents belong to the product checks."""
    blocks = markdown.split("\n\n")
    layout = []
    for i, item in enumerate(product.items):
        title, reason = f"items.{i}.title", f"items.{i}.rationale"
        lines = blocks[i].splitlines() if i < len(blocks) else []
        if lines[:2] != [f"**{_text(item.title)}**", f"选材理由：{_text(item.rationale)}"]:
            for j, other in enumerate(product.items):
                if j != i and lines[:2] == [f"**{_text(item.title)}**", f"选材理由：{_text(other.rationale)}"]:
                    raise LayoutMismatch(f"第 {i+1} 项标题下实际放置了第 {j+1} 项的选材理由。")
            raise LayoutMismatch(f"第 {i+1} 个标题或紧随其后的选材理由与已审条目无法绑定。")
        layout.append({"bold_title":title, "rationale":reason})
    gap_lines = ["未核实或未覆盖：", *["- " + _text(gap) for gap in product.gaps]]
    receipts = set()
    for block in blocks[len(product.items):]:
        if product.gaps and block.splitlines() == gap_lines:
            layout.append({"gap_list":[f"gaps.{i}" for i in range(len(product.gaps))]})
        elif block.startswith(("当轮取材窗口与观测覆盖：\n", "取材投稿署名：\n", "以下给定来源尚无网页正文回执：\n")):
            header, *rows = block.splitlines()
            if header in receipts or not rows or any(not row.startswith("- ") for row in rows):
                raise LayoutMismatch("资料区重复，或含有不属于该区的正文段落。")
            receipts.add(header)
            layout.append({"receipt_section":"list"})
        else:
            raise LayoutMismatch("成品多出条目，或单独的缺口列表与所列缺口无法绑定。")
    if sum("gap_list" in group for group in layout) != int(bool(product.gaps)):
        raise LayoutMismatch("写作段落与实际成品的独立缺口列表无法绑定。")
    return layout


# Match whole directives only. Qualifiers or other editorial requirements stay in the review.
_GROUP = r"(?:group|organize|keep)\s+(?:closely\s+)?related\s+(?:points|ideas|topics|details|information)(?:\s+together)?"
_SEPARATE = r"separate\s+(?:distinct|different|unrelated)\s+(?:concerns|topics|ideas|points)"
_GROUPING = re.compile(r"(?:" + _GROUP + r"(?:\s+and\s+" + _SEPARATE + r")?|" + _SEPARATE
                       + r"|(?:将|把)?相关(?:内容|要点|主题)(?:放在一起|归为一组|分组))[.。]?", re.I)
_READABILITY = re.compile(r"use (?:headings and lists|lists and headings) (?:when|where) "
                          r"(?:they (?:improve|aid) readability|helpful)[.]?", re.I)


def layout_clauses(clauses):
    covered, remaining = [], []
    for text in clauses:
        sentences = re.split(r"(?<=[.。])\s+|(?<=。)(?=[^\s])", text)
        residual = [s for s in sentences if not (_GROUPING.fullmatch(s) or _READABILITY.fullmatch(s))]
        if not any(_GROUPING.fullmatch(s) for s in sentences) or any(
            re.search(r"\b(?:unless|except|instead|if|only|however)\b|仅当|除非|如果|但是", s, re.I) for s in residual
        ):
            remaining.append(text)
            continue
        for sentence in sentences:
            (covered if _GROUPING.fullmatch(sentence) or _READABILITY.fullmatch(sentence) else remaining).append(sentence)
    return covered, remaining


def check_item_groups(product, markdown, evidence, record):
    """Compare approved editorial fields and bound sources with their actual presentation."""
    layout = writing_layout(product, markdown)
    record["layout"] = layout
    if not product.items and product.gaps:
        record.update(bindings=[], basis="已提交的缺口逐项对应独立列表")
        return {}
    if not product.items or evidence is None:
        raise DelegationError("缺少已绑定的条目或来源，不能确认全文分组。")
    blocks = markdown.split("\n\n")
    primaries = [primary_frame(item, evidence) for item in product.items]
    source_texts = [[evidence[key] for key in item.sources] for item in product.items]
    bindings = record["bindings"] = []
    misplaced = {}
    for i,item in enumerate(product.items):
        title, rationale = f"items.{i}.title", f"items.{i}.rationale"
        primary = primaries[i]
        tail = blocks[i].splitlines()[2:]
        if primary:
            if len(tail) < 2 or tail[0] != "主帧：" + evidence.display_source(primary) or not tail[1].startswith("覆盖："):
                raise LayoutMismatch(f"第 {i+1} 个标题下的主帧回执与已绑定来源不对应。")
            tail = tail[2:]
        citations = list(dict.fromkeys(key for key in item.sources if key != primary))
        if tail != ["- " + (evidence.display_source(k) if primary else evidence[k]) for k in citations]:
            raise LayoutMismatch(f"第 {i+1} 个标题下的资料回执与已绑定来源不对应。")
        binding = {"title":title, "rationale":rationale, "sources":item.sources, "primary_frame":primary}
        bindings.append(binding)
        if len(product.items) == 1:
            binding["basis"] = "唯一已审条目，标题、理由及回执在同一块内"
            continue
        # A verified field receipt is stronger than trying to match paraphrased prose.
        proofs = {path:product._field_sources.get(path, {}) for path in (title, rationale)}
        if all(proof.get("source") in item.sources and proof.get("observation") and
               all(proof["source"] not in other.sources for j,other in enumerate(product.items) if j != i)
               for proof in proofs.values()):
            binding["field_sources"] = proofs
            continue
        # For verbatim materials use the source text independently of the submitted item.
        matches = binding["source_spans"] = []
        for path, text in ((title,item.title), (rationale,item.rationale)):
            for part in re.split(r"(?<=[。！？!?])|(?<=\.)\s+|[\n；;]+", text):
                part = part.strip()
                if not part:
                    continue
                owners = [j for j,sources in enumerate(source_texts)
                          if any(c.isalnum() for c in part) and any(part in source for source in sources)]
                matches.append({"field":path,"text":part,"matching_items":owners})
                if len(owners) != 1:
                    raise DelegationError("缺少可区分各条目的字段来源，不能确认正文归属。")
                if owners != [i]:
                    misplaced.setdefault(path, []).append(part)
    return misplaced


def bind_judgment(judgment, clauses, shown, targets, scope):
    if {d.clause for d in judgment.decisions} != clauses.keys():
        raise DelegationError("写作结论未逐项对应本轮子句。")
    seen = {}
    defects, blockers = [], []
    for d in judgment.decisions:
        if (not d.reason.strip() or d.verdict == "unknown" or not set(d.fields) <= shown.keys()
                or (d.verdict == "revise_fields" and not set(d.fields) <= targets.keys())
                or (scope and d.verdict == "layout_conflict")):
            raise DelegationError("写作结论依据不足或超出可审范围。")
        prior = seen.setdefault(d.clause, set())
        prior.add(d.verdict)
        if "meets" in prior and len(prior) > 1:
            raise DelegationError("同一写作子句的判断相互矛盾。")
        evidence = {"clause":d.clause, "owner_excerpt":clauses[d.clause], "reason":d.reason}
        if d.verdict == "revise_fields":
            defects.extend(evidence | {"path":path,"field_excerpt":shown[path]} for path in dict.fromkeys(d.fields))
        elif d.verdict == "layout_conflict":
            blockers.append(evidence | {"fields":d.fields})
    return defects, blockers


def semantic_clauses(note, checks, title_form):
    """Remove only complete directives backed by checks actually run on this draft."""
    if _CONDITIONAL.search(re.sub(_QUOTED, "", note)):
        return [note]
    literal_ends = {(b["end"], b["check"].field) for b in literal_evidence(note) if b["check"] in checks}
    remaining = []
    field = None
    covered = False
    for begin, end in spans(note, "，,。！？；;\n"):
        clause = note[begin:end].strip()
        if not clause or (field is None and _GENRE.fullmatch(clause)):
            continue
        directive = clause
        if subject := _SUBJECT.match(clause):
            field = _FIELD[subject[1]]
            directive = clause[subject.end():]
        directive = directive[_MODIFIERS.match(directive).end():]
        if ((begin + len(note[begin:end].rstrip()), field) in literal_ends and
                any(pattern.fullmatch(directive) for pattern in (_BOUNDARY, _CONTAINS, _LENGTH))):
            covered = True
            continue
        form = _FORM.fullmatch(directive)
        if field == "title" and form and title_form == ("问句" if form[1] == "疑问句" else form[1]):
            covered = True
            continue
        remaining.append(clause)
        if not form:
            field = None
    return remaining if covered else [note]


async def review_preferences(notes, checks, product, call, locked, *, markdown, audit,
                             title_forms=None, allow_defer=False, evidence=None):
    authored = editorial_fields(product)
    fields = {k:v for k,v in authored.items() if k not in locked}
    issues = {}
    for identity, check in checks:
        if identity not in notes:
            raise DelegationError("写作检查引用了本轮未选的偏好，尚未交付。")
        for path, text in fields.items():
            field = "gap" if path.startswith("gaps.") else path.rsplit(".", 1)[1]
            if field == check.field and not check.meets(text):
                issues.setdefault(path, set()).add(identity)
    repair_literals = allow_defer and bool(issues)
    for identity, note in notes.items():
        scope = scoped_fields(note)
        targets = {path: text for path, text in fields.items()
                   if scope is None or ("gap" if path.startswith("gaps.") else path.rsplit(".", 1)[1]) in scope}
        if scope and not targets:
            continue
        bound_checks = [check for source, check in checks if source == identity]
        form = (title_forms or {}).get(identity)
        clauses = semantic_clauses(note, bound_checks, form)
        entry = {"id":identity, "scope":sorted(scope) if scope else "document", "paths":list(targets),
                 "literal_failures":[p for p in targets if identity in issues.get(p, ())],
                 "review_clauses":clauses, "violations":[]}
        audit.append(entry)
        if not clauses and (bound_checks or form):
            entry["basis"] = "约定的完整子句由已绑定字面检查或独立标题句式审读覆盖"
            continue
        covered, remaining = layout_clauses(clauses)
        if covered:
            record = entry["item_group_check"] = {"owner_clauses":covered}
            try:
                misplaced = check_item_groups(product, markdown, evidence, record)
            except DelegationError as error:
                record.update(status="violated" if isinstance(error, LayoutMismatch) else "unverified", reason=str(error))
                if not repair_literals:
                    raise
                entry["deferred"] = "已确认的字面问题返修后，重新核对条目分组"
            else:
                record["status"] = "violated" if misplaced else "verified"
                for path, parts in misplaced.items():
                    if path in locked:
                        raise DelegationError("分组约定涉及已锁定的字段，尚未交付。")
                    issues.setdefault(path, set()).add(identity)
                    entry["violations"].extend({"path":path,"owner_excerpt":covered[0],
                                                "field_excerpt":part,"reason":"该段文字只绑定到另一个标题的来源"}
                                               for part in parts)
        if not remaining:
            continue
        clauses = {f"C{i}":text for i,text in enumerate(remaining, 1)}
        shown = authored if scope is None else targets
        if not shown:
            raise DelegationError("本轮没有可供核对写作约定的正文。")
        packet = {"owner_clauses":clauses, "delivery_fields":shown}
        if list(clauses.values()) != [note]:
            packet["owner_preference_context"] = note
        if scope is None:
            packet["layout"] = writing_layout(product, markdown)
            if locked:
                packet["editable_fields"] = list(targets)
        entry["review_input"] = packet.copy()
        schema = WritingJudgment.model_json_schema()
        props = schema["$defs"]["ClauseVerdict"]["properties"]
        props["clause"]["enum"] = list(clauses)
        props["fields"]["items"]["enum"] = list(shown)
        if scope:
            props["verdict"]["enum"].remove("layout_conflict")
        instruction = ("只审 owner_clauses 中的写作要求。按正文和段落分组逐条判断，选择子句和字段 ID，"
                       "说明具体文字怎样符合或违反该条要求。依据不足选 unknown。")
        for attempt in range(2):
            result = None
            try:
                with without_model_identity():
                    result = await call([{"role":"system","content":instruction},
                                         {"role":"user","content":dump(packet)}],schema)
                    judgment = WritingJudgment.model_validate(result)
                    defects, blockers = bind_judgment(judgment, clauses, shown, targets, scope)
                    verdict = JudgmentCheck.model_validate(await call([{"role":"system","content":
                        "独立核对每项结论。理由须从指定子句和实际字段推出判定，并解释符合或违反之处。"
                        "遗漏明显反例、字段不对应、另加要求或证据不足时，supported 为 false 并指出原因。"},
                        {"role":"user","content":dump(entry["review_input"] | {"decisions":result["decisions"]})}],
                        JudgmentCheck.model_json_schema()))
                    entry.setdefault("judgment_checks", []).append(verdict.model_dump())
                    if not verdict.supported or not verdict.reason.strip():
                        raise DelegationError("写作结论与所引子句、段落缺少对应依据。")
            except (DelegationError, ValidationError, InferenceBudgetExceeded) as error:
                reason = str(error) if isinstance(error, DelegationError) else "写作审稿未按字段结构返回"
                entry.setdefault("rejected_reviews", []).append({"result":result, "reason":reason, "error":type(error).__name__})
                if repair_literals or entry["violations"]:
                    entry["deferred"] = "已确认的字段仍需返修，未完成的写作判断须随修订稿复核"
                    break
                if attempt:
                    if isinstance(error, InferenceBudgetExceeded):
                        raise
                    raise DelegationError(reason) from error
                packet["review_feedback"] = reason
                if entry.get("judgment_checks") and not entry["judgment_checks"][-1]["supported"]:
                    packet["review_feedback"] += entry["judgment_checks"][-1]["reason"]
                continue
            entry.update(judgment.model_dump(), layout_issues=blockers)
            entry["violations"].extend(defects)
            for defect in defects:
                issues.setdefault(defect["path"], set()).add(identity)
            if blockers:
                if not repair_literals:
                    raise DelegationError(f"成品写作约定 #{identity} 有无法以字段修订的冲突，尚未交付。")
                entry["deferred"] = "字面返修后重新审查"
            break
    return {path:sorted(ids) for path,ids in issues.items()}
