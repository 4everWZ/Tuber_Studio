"""Verify named people separately from a frame's visible composition."""

import re
from typing import Literal

from pydantic import Field

from ..models import StrictModel
from ..store import dump
from ..web_fetch import programme_credit_text
from .task_delivery import DelegationError


class PersonReference(StrictModel):
    phrase: str = Field(min_length=1, description="字段内逐字包含人名或归属主张的完整短语。")
    person: str = Field(min_length=1, description="短语中的姓名、昵称或被明确指认为物主的人物。")
    relation: Literal["投稿署名", "节目导演", "画面文字提名", "画中人物身份", "场景或物件归属", "其它具名关系"]


async def extract_people(fields, generate, budget):
    subjects = ["地点、构图或物件", "未指认身份的人物外观", "编辑安排或其它表述", "含具体人物的姓名、昵称、署名或身份归属"]
    subject_schema = {"type": "object", "properties": {"person_subject": {"type": "string", "enum": subjects}},
                      "required": ["person_subject"], "additionalProperties": False}
    kinds = ["人名、专属昵称、明确指向特定人的名号", "职务或群体的泛称", "服饰、位置、动作等外观指称", "其他实体或一般表述"]
    span_schema = {"type":"object","properties":{"span":{"type":"string"},"kind":{"type":"string","enum":kinds}},
                   "required":["span","kind"],"additionalProperties":False}
    result = {}
    for path, text in fields.items():
        field = {"role": "user", "content": text}
        subject = await generate([{"role": "system", "content": "按这一句话所述对象选择类别。只在明确写了人名、"
            "具体人物身份或指认物主时选人物归属。兼有两类时优先标记具名关系。"}, field], subject_schema, max_tokens=budget)
        if subject.get("person_subject") not in subjects:
            raise DelegationError("人物实体审读没有返回有效的字段分类。")
        if subject["person_subject"] == subjects[-1]:
            spans = await generate([{"role":"system","content":"对人物指代表达做实体分析。分别标注专名和一般指称。"
                "含姓名的长定语只截出姓名，不把衣着、头饰、动作或‘人物’附在人名上。只有外观时按外观指称标注。"},
                field], {"type":"object","properties":{"mentions":{"type":"array","items":span_schema}},
                         "required":["mentions"],"additionalProperties":False},max_tokens=budget)
            names = {s["span"] for s in spans["mentions"] if s["kind"] == kinds[0] and s["span"] and s["span"] in text}
            if names:
                reference = PersonReference.model_json_schema()
                reference["properties"]["person"]["enum"] = sorted(names)
                reply = await generate([{"role":"system","content":"逐字定位字段中每位具名人物的完整关系短语。"}, field],
                    {"type":"object","properties":{"person_references":{"type":"array","items":reference}},
                     "required":["person_references"],"additionalProperties":False},max_tokens=budget)
                references = [PersonReference.model_validate(c) for c in reply["person_references"] if c["person"] in names]
                located = {r.person for r in references if r.phrase in text and r.person in r.phrase}
                references.extend(PersonReference(phrase=n,person=n,relation="其它具名关系") for n in names - located)
                result[path] = references
            else:
                result[path] = []
        else:
            result[path] = []
    return result


def person_credits(steps, item, evidence, primary):
    point = evidence.points[primary]
    byline = evidence.part_bylines.get((point.bvid, point.part, point.cid))
    credits = []
    for s in steps:
        if s["tool"] != "web_page" or s["id"] not in item.sources:
            continue
        r = s.get("result") or {}
        if not r.get("excerpt") or r.get("error"):
            continue
        prefix = f"投稿《{r.get('title')}》，UP 主："
        if r.get("source_kind") == "bilibili_public_metadata" and byline and evidence.video_byline.get(s["id"]) == byline:
            name, separator, _ = r["excerpt"].removeprefix(prefix).partition("。合集 P")
            if separator and name and r["excerpt"].startswith(prefix):
                credits.append({"person": name, "relation": "投稿署名", "source": s["id"], "url": r["url"]})
        for names in re.findall(r"(?:^|；)导演：([^；]+)", programme_credit_text(r) or ""):
            for name in re.split(r"[、，,／/]", names):
                if name.strip():
                    credits.append({"person": name.strip(), "relation": "节目导演", "source": s["id"], "url": r["url"]})
    return credits


def check_people(path, text, references, *, editor, primary, credits, screen_text):
    claims = []
    unlocated = []
    for ref in map(PersonReference.model_validate, references):
        if ref.person not in text or ref.phrase not in text:
            unlocated.append(ref.model_dump())
            continue
        supported = [c for c in credits if c["person"] == ref.person and c["relation"] == ref.relation]
        on_screen = ref.relation == "画面文字提名" and any(ref.person in s for s in screen_text)
        if ref.person not in ref.phrase:
            issue = "审稿未逐字定位该人名及其关系。"
        elif supported or on_screen:
            issue = None
        else:
            issue = "本项没有该人物与所述身份、场景或物件关系的回执。"
        claims.append(ref.model_dump() | {"sources": supported, "issue": issue})
    # The current editor can never be its own source for a name in a visual field.
    for name in {editor, *(c["person"] for c in credits)} - {None, ""}:
        spans = [m.span() for c in claims if c["person"] == name for m in re.finditer(re.escape(c["phrase"]), text)]
        for mention in re.finditer(re.escape(name), text):
            if not any(start <= mention.start() and end >= mention.end() for start, end in spans):
                claims.append({"phrase": name, "person": name, "offset": mention.start(), "relation": "未核对的具名关系",
                               "issue": "本项未核对此处人名的身份或归属。", "sources": []})
    return {"field": path, "text": text, "primary_frame": primary, "claims": claims,
            "unlocated": unlocated, "pages": credits}


async def check_screen_title(path, title, primary, image, screen_text, bylines, programme_names, generate, budget):
    """An observed string supports transcription. Using it as an identity requires an independent credit."""
    fragments = list(dict.fromkeys(line.strip() for text in screen_text for line in text.splitlines()
                                   if line.strip() and line.strip() in title))
    if not screen_text:
        return None
    categories = ["具体物件、字形、构图或视觉载体", "以内容主题、学科或艺术门类命名", "其他句式或剪辑安排"]
    subject = await generate([{"role":"system","content":"只判断这个标题本身为素材取了什么名字。"
        "一个学科或艺术门类的名称属于主题。具体的文字对象、字体形状、标识或其外观属于可见载体。"
        "若以主题命名，逐字摘取这个主题词。"},
        {"role":"user","content":title}],{"type":"object","properties":{"title_subject":{"type":"string","enum":categories},
        "subject_words":{"type":"string"}},"required":["title_subject","subject_words"],"additionalProperties":False},max_tokens=budget)
    claims = []
    if subject["title_subject"] == categories[1]:
        words = subject["subject_words"]
        if not words or words not in title:
            raise DelegationError("主题审读未定位标题内的实际主张。")
        proof = ["主帧直接展示该主题的内容或实践", "只能看到字形、图形设计或局部字样", "内容无法辨明"]
        checked = await generate([{"role":"system","content":"核对这幅图能否独立支持用所述主题命名素材。"
            "用某种字形排印标识，仅说明标识的外观。将主帧表现的实物或实践与渲染文字的方式分别判断。"},
            {"role":"user","content":[{"type":"text","text":dump({"候选主题":words})},image]}],
            {"type":"object","properties":{"topic_evidence":{"type":"string","enum":proof}},
             "required":["topic_evidence"],"additionalProperties":False},max_tokens=budget)
        claims.append({"phrase":words,"target":words,"relation":"主帧内容主题","observation":checked["topic_evidence"],
                       "issue":None if checked["topic_evidence"] == proof[0] else "可见的字形或图形不足以把该主题赋给片段。"})
    receipt = {"field":path,"text":title,"primary_frame":primary,"claims":claims,"pages":bylines,
               "programme_names":sorted(programme_names),"screen_text":screen_text}
    if not fragments:
        return receipt
    roles = ["字形或构图描述，未用屏字指称主体", "明确转录当前可见的字", "以屏字命名主体或作品", "把字形或残字解释为节目内容主题"]
    schema = {"type":"object","properties":{f:{"type":"string","enum":roles} for f in fragments},
              "required":fragments,"additionalProperties":False}
    reply = await generate([{"role":"system","content":"逐项判断标题怎样使用图中的文字。画面上能读出几个字，仅能证明这些字出现。"
        "题首直接用一串字为作品或主体命名属于命名。将字体样式说成所讲内容属于主题断言；"
        "只说字的颜色或笔形属于视觉描述。"},
        {"role":"user","content":[{"type":"text","text":dump({"实际标题":title,"与屏字相同的部分":fragments})}, image]}],
        schema,max_tokens=budget)
    if set(reply) != set(fragments) or any(use not in roles for use in reply.values()):
        raise DelegationError("屏字用途审查没有逐项返回有效判断。")
    names = {c["person"] for c in bylines if c["relation"] == "投稿署名"} | set(programme_names)
    for fragment, use in reply.items():
        issue = None
        if use == roles[2] and fragment not in names:
            issue = "主帧屏字未获完整投稿或节目署名支持，不能据此命名主体。"
        if use == roles[3]:
            issue = "屏字外形或局部文字无法证明节目的内容主题。"
        claims.append({"phrase":fragment,"target":fragment,"relation":use,"issue":issue})
    return receipt
