import json

import pytest

from tests.support.agent_helpers import reviewed_window as reviewed_window
from tests.support.agent_helpers import selection, short_read
from tuber_studio.agent.tasks.delivery import (
    DelegationError,
    PrivateDelivery,
    evidence_index,
    render_private_delivery,
)
from tuber_studio.agent.tasks.editor_contract import (
    FormatCheck,
    layout_clauses,
    literal_checks,
    literal_evidence,
    review_preferences,
    scoped_fields,
    semantic_clauses,
)
from tuber_studio.agent.tasks.tools import video_grounding


def rendered_sample(product):
    evidence = evidence_index([])
    evidence.update({s:"取材回执" for item in product.items for s in item.sources})
    return render_private_delivery(product, evidence)[1]


def decision(verdict, fields, reason, clause="C1"):
    return {"decisions":[{"clause":clause, "fields":list(fields), "verdict":verdict, "reason":reason}]}


async def test_private_contract_merges_literal_and_semantic_issues_and_preserves_locked_fields():
    product = PrivateDelivery(items=[{"title":"旧标题", "rationale":"旧理由", "sources":["frame"]},
                                     {"title":"试播", "rationale":"先介绍来龙去脉，再给结论", "sources":["frame"]}], gaps=[])
    notes = {2:"标题统一以「素材」开头，理由先给结论。", 5:"每个理由最多六个字。"}
    checks = [(2, FormatCheck(field="title", operation="starts_with", value="素材")),
              (5, FormatCheck(field="rationale", operation="max_chars", value="6"))]
    async def review(messages, schema):
        packet = json.loads(messages[-1]["content"])
        if schema["title"] == "JudgmentCheck":
            return {"supported":True, "reason":"字段和子句对应"}
        if packet.get("owner_preference_context") == notes[2]:
            assert set(packet["editable_fields"]) == {"items.1.title","items.1.rationale"}
            return decision("revise_fields", ["items.1.rationale"], "结论落在背景之后")
        assert set(packet["delivery_fields"]) == {"items.1.rationale"}
        return decision("meets", ["items.1.rationale"], "核对了理由长度")
    options = {"markdown":rendered_sample(product), "audit":[]}
    issues = await review_preferences(notes, checks, product, review, {"items.0.title","items.0.rationale"}, **options)
    assert issues == {"items.1.title":[2], "items.1.rationale":[2,5]}
    with pytest.raises(DelegationError, match="未选"):
        await review_preferences(notes, [(100, checks[0][1])], product, review, set(), **options)


@pytest.mark.parametrize("note", ["标题都用问句并将全文分组", "标题都用问句，理由给出依据。",
                                  "Group related points. Use headings when helpful.", "見出しを工夫する。"])
def test_unbound_writing_directions_keep_document_context(note):
    assert scoped_fields(note) is None


@pytest.mark.parametrize("outcome", ["field", "pass", "document", "foreign_path", "foreign_clause", "foreign_blocker", "unknown"])
async def test_two_item_scopes_on_rendered_delivery(outcome):
    # Synthetic submissions exercise the two original directions. The rejected drafts were not retained.
    notes = {1:"Arrange the discussion around the individual topics. Use headings and lists when helpful.",
             2:"以后写直播素材提纲，每个标题都用问句，以问号结尾。"}
    reads = [short_read("first", 1), short_read("second", 2)]
    evidence = evidence_index(reads)
    second_title = "窗边是什么灯？" if outcome == "pass" else "窗边的小灯"
    product = PrivateDelivery(items=[
        {"title":title, "rationale":reason, "sources":[frame], "primary_frame":frame}
        for title, reason, frame in [("桌上有什么？", "记录桌上的物件。", "first:1"),
                                     (second_title, "记录光线，又跳回桌面。", "second:1")]], gaps=[])
    _, markdown = render_private_delivery(product, evidence)
    audit = []

    async def review(messages, schema):
        packet = json.loads(messages[-1]["content"])
        assert "goal" not in packet and "取材窗口" not in str(packet) and "主帧：" not in str(packet)
        if schema["title"] == "JudgmentCheck":
            return {"supported":True, "reason":"指定理由说明了这项偏好"}
        props = schema["$defs"]["ClauseVerdict"]["properties"]
        if packet["owner_clauses"] == {"C1":notes[1]}:
            assert packet["layout"][1]["rationale"] == "items.1.rationale"
            assert packet.keys() == {"owner_clauses","layout","delivery_fields"}
            if outcome == "field":
                return decision("revise_fields", ["items.1.rationale"], "第二项混入第一项主题")
            if outcome == "document":
                return decision("layout_conflict", ["items.0.title"], "分组问题只能在排版层修正")
            return decision("meets", props["fields"]["items"]["enum"], "两项已分段")
        assert "layout" not in packet and set(packet["delivery_fields"]) == {"items.0.title","items.1.title"}
        assert "layout_conflict" not in props["verdict"]["enum"]
        verdict = {"pass":"meets","unknown":"unknown","foreign_blocker":"layout_conflict"}.get(outcome,"revise_fields")
        return decision(verdict, ["items.1.rationale" if outcome == "foreign_path" else "items.1.title"],
                        "检查了标题句式", "C2" if outcome == "foreign_clause" else "C1")

    options = {"audit":audit, "markdown":markdown}
    checks = [(2, check) for check in literal_checks(notes[2])]
    if outcome in {"pass", "field"}:
        issues = await review_preferences(notes, checks, product, review, set(), **options)
        assert issues == ({} if outcome == "pass" else {"items.1.title":[2], "items.1.rationale":[1]})
        assert [entry["scope"] for entry in audit] == ["document", ["title"]]
    else:
        with pytest.raises(DelegationError, match={"document":"无法以字段修订","foreign_clause":"本轮子句"}.get(outcome,"可审范围")):
            await review_preferences(notes, checks, product, review, set(), **options)


def test_coverage_requires_complete_executable_clauses():
    note = "以后写直播素材提纲，每个标题都用问句，以问号结尾。"
    checks = literal_checks(note)
    assert semantic_clauses(note, checks, "问句") == []
    assert semantic_clauses(note, checks, None) == ["每个标题都用问句"]
    assert semantic_clauses(note, [], "问句") == ["以问号结尾"]
    assert semantic_clauses(note+"理由要解释取材价值。", checks, "问句") == ["理由要解释取材价值"]
    assert semantic_clauses("标题都用问句，同时解释原因。", [], "问句") == ["同时解释原因"]
    conditional = "如果涉及赛果，每个标题用问句，以问号结尾。"
    assert semantic_clauses(conditional, checks, "问句") == [conditional]
    mixed = "Organize related topics. Explain each abbreviation."
    assert layout_clauses([mixed]) == (["Organize related topics."],["Explain each abbreviation."])
    qualified = "Organize related topics. Only do this for the appendix."
    assert layout_clauses([qualified]) == ([],[qualified])


async def test_unbound_rendering_is_not_presented_as_a_verified_group():
    product = PrivateDelivery(items=[{"title":"开场", "rationale":"第一组", "sources":["f"]}], gaps=["第二组"])
    async def unexpected(*_):
        pytest.fail("The writing surface does not match the submitted Markdown")
    for original in ("选材理由：第一组", "- 第二组"):
        with pytest.raises(DelegationError, match="无法绑定"):
            await review_preferences({4:"Group related points."}, [], product, unexpected, set(),
                                     markdown=rendered_sample(product).replace(original, "错放段落"), audit=[])


@pytest.mark.parametrize("change", ["none","swap_reasons","mix_sentences","swap_rendered_reasons",
                                    "swap_sources","lose_group","duplicate_group","duplicate_gaps","unbound"])
async def test_bound_item_layout_with_same_titles_and_distinct_sources(change):
    note = "Group related points and separate distinct concerns. Use headings and lists when they improve readability."
    titles = ["盆栽怎样换盆？","机票怎样退票？"]
    reasons = ["先给新盆铺土。移栽后给根部浇水。","先找到机票订单。提交退款申请后查到账记录。"]
    evidence = evidence_index([{"id":str(i), "tool":"owner_attachment", "result":{"name":"说明","text":title+reason}}
                               for i,(title,reason) in enumerate(zip(titles,reasons,strict=True))])
    product = PrivateDelivery(items=[{"title":t,"rationale":r,"sources":[f"{i}:0"]} for i,(t,r) in
                                     enumerate(zip(titles,reasons,strict=True))],gaps=["待补价格"])
    if change == "swap_reasons":
        product.items[0].rationale, product.items[1].rationale = reversed(reasons)
    if change == "mix_sentences":
        product.items[0].rationale = "先给新盆铺土。先找到机票订单。"
        product.items[1].rationale = "移栽后给根部浇水。提交退款申请后查到账记录。"
    if change == "unbound":
        product.items[0].rationale = "没有独立出处的文字"
    markdown = render_private_delivery(product,evidence)[1]
    blocks = markdown.split("\n\n")
    if change in {"swap_rendered_reasons","swap_sources"}:
        a,b = (block.splitlines() for block in blocks[:2])
        pos = 1 if change == "swap_rendered_reasons" else 2
        a[pos], b[pos] = b[pos], a[pos]
        blocks[:2] = ["\n".join(a), "\n".join(b)]
    elif change == "lose_group":
        blocks.pop(1)
    elif change in {"duplicate_group","duplicate_gaps"}:
        blocks.append(blocks[0 if change == "duplicate_group" else -1])
    async def no_semantic_vote(*_):
        pytest.fail("A complete grouping contract is checked against the artifact and its sources.")
    audit = []
    async def review():
        return await review_preferences({1:note}, [], product, no_semantic_vote, set(), markdown="\n\n".join(blocks),
                                        evidence=evidence,audit=audit)
    if change == "none":
        assert await review() == {}
        assert audit[0]["item_group_check"]["status"] == "verified"
    elif change in {"swap_reasons","mix_sentences"}:
        assert await review() == {"items.0.rationale":[1],"items.1.rationale":[1]}
        assert audit[0]["item_group_check"]["status"] == "violated"
    else:
        with pytest.raises(DelegationError):
            await review()
        assert audit[0]["item_group_check"]["status"] == ("unverified" if change == "unbound" else "violated")


@pytest.mark.parametrize("recover", [False, True])
async def test_unbound_review_cannot_preempt_two_literal_repairs_or_authorize_delivery(recover):
    title_note = "以后写直播素材提纲，每个标题都用问句，以问号结尾。"
    notes = {1:"Arrange the discussion around the individual topics.", 2:title_note}
    product = PrivateDelivery(items=[{"title":title, "rationale":"观察", "sources":["f"]}
                                     for title in ("人物的衣饰", "中央摆着座椅")], gaps=[])
    attempts = []
    async def reviewer(messages, schema):
        packet = json.loads(messages[-1]["content"])
        assert packet["owner_clauses"] == {"C1":notes[1]}
        if schema["title"] == "JudgmentCheck":
            good = packet["decisions"][0]["reason"] == "两项主题已经分开"
            return {"supported":good, "reason":"组织依据已给出" if good else "授权与组织要求无关"}
        attempts.append(packet)
        if recover and "review_feedback" in packet:
            return decision("meets", ["items.0.rationale","items.1.rationale"], "两项主题已经分开")
        return decision("revise_fields", ["items.1.title"], "归档授权及剧透")
    checks = [(2,c) for c in literal_checks(title_note)]
    audit = []
    async def review(first):
        return await review_preferences(notes, checks, product, reviewer, set(), markdown=rendered_sample(product),
                                        audit=audit, title_forms={2:"问句"}, allow_defer=first)
    assert await review(True) == {"items.0.title":[2],"items.1.title":[2]}
    assert len(attempts) == 1 and audit[0]["deferred"] and not audit[0]["violations"]
    assert audit[1]["literal_failures"] == ["items.0.title","items.1.title"]
    product.items[0].title, product.items[1].title = "衣饰是什么颜色？", "座椅在哪里？"
    if recover:
        assert await review(False) == {}
    else:
        with pytest.raises(DelegationError, match="对应依据"):
            await review(False)
    assert len(attempts) == 3 and "review_feedback" in attempts[-1]


def test_private_punctuation_and_codepoint_length_checks():
    suffix = FormatCheck(field="title", operation="ends_with", value="？")
    assert suffix.meets("这是哪里?") and not suffix.meets("宫廷场景")
    size = suffix.model_copy(update={"operation":"max_chars","value":"1"})
    assert size.meets("Ⅳ") and not size.meets("IV")


@pytest.mark.parametrize(("note","expected"), [
    ("以后写直播素材提纲，每个标题都用问句，以问号结尾。", [("title","ends_with","？")]),
    ("以后直播提纲的标题都用陈述句，以句号结尾。", [("title","ends_with","。")]),
    ("标题以「本周」开头，理由最多20个字。", [("title","starts_with","本周"),("rationale","max_chars","20")]),
    ("缺口须包含“待核实”。", [("gap","contains","待核实")]),
    ("如果是开播预告，标题可以以问号结尾。", []),
    ("第二个标题以问号结尾。", []), ("标题不要以问号结尾。", []),
    ("标题讨论以问号结尾的商标。", []), ("标题不要求包含“独家”。", []),
    ("标题以？结尾。", [("title","ends_with","？")]),
    ("标题以「甲，乙」开头。", [("title","starts_with","甲，乙")]),
])
def test_unambiguous_owner_field_literals(note, expected):
    assert [(c.field,c.operation,c.value) for c in literal_checks(note)] == expected
    for binding in literal_evidence(note):
        assert binding["quote"] == note[binding["start"]:binding["end"]]


@pytest.mark.parametrize(("phase","title","form","core","observation","punctuation","passed"), [
    ("voice_corrected","皇帝宝座的华丽装饰。","名词短语","装饰","宝座在中央",True,False),
    ("voice_corrected","宝座在中央。","陈述句","在中央","宝座在中央",True,True),
    ("voice_corrected","宝座在中央","陈述句","在中央","宝座在中央",False,False),
    ("voice_corrected","宝座象征权力。","陈述句","象征权力","无法核实",True,False),
    ("voice_corrected","宝座的装饰。","陈述句","呈金色","宝座在中央",True,False),
    ("before","宝座在什么位置？","问句","什么位置","宝座在中央",True,True),
    ("before","宝座在中央？","陈述句","在中央","宝座在中央",True,False),
    ("after","中央的宝座","名词短语","宝座","宝座在中央",True,True),
])
async def test_three_edition_measurement_separates_form_punctuation_and_grounding(
        phase, title, form, core, observation, punctuation, passed):
    from evaluations.memory.owner_memory_video import measure_edition

    async def read(messages, schema):
        packet = json.loads(messages[-1]["content"])
        assert packet == {"title_words":title.rstrip("。？"), "observations":["宝座在中央"]}
        return {"form":form, "core":core, "supported":True, "observation":observation,
                "answer":"中央" if form == "问句" else ""}

    report = await measure_edition([{"title":title,"primary_frame":"frame"}], {"frame":{"details":["宝座在中央"]}}, phase, read)
    assert report["items"][0]["punctuation_matches"] is punctuation
    assert report["passed"] is passed


async def test_editor_selects_a_sentence_against_one_observation_for_the_original_frame_review():
    from tuber_studio.agent.tasks.title_form import grounded_options

    fact = "白色台灯位于书桌右侧"
    async def call(messages, schema):
        data = json.loads(messages[-1]["content"])
        if "observations_by_id" in data:
            return {"0":"","1":"位于书桌右侧"}
        if "frame_details" in data:
            assert data["frame_details"] == [fact]
            return {"fact":fact, "options":[{"evidence_span":"位于书桌右侧","title":t} for t in
                               ("书桌右侧的白色台灯。","台灯在书桌右侧。","书桌右侧有一盏白色台灯。")]}
        if "statement" in data:
            assert data == {"statement":"台灯在书桌右侧", "frame_observation":fact}
            return {"same_relations":True, "parts":[{"role":role, "words":words, "basis":basis, "entailed":True} for role,words,basis in
                             [("实体","台灯","白色台灯"),("关系","在","位于"),("场所","书桌右侧","书桌右侧")]]}
        assert data["observations"] == [fact] and "writing_preference" not in data
        phrase = data["title_words"].endswith("台灯")
        return {"form":"名词短语" if phrase else "陈述句", "core":"台灯" if phrase else "在书桌右侧",
                "observation":fact, "supported":True, "answer":""}
    review = await grounded_options(["白色台灯", fact], "陈述句",
                                    [FormatCheck(field="title", operation="ends_with",value="。")], call)
    assert review["chosen"] == "台灯在书桌右侧。"
    assert review["candidates"][0]["reading"]["form"] == "名词短语"
    assert review["candidates"][1]["reading"]["grounded"]


async def test_anchored_question_candidate_must_ask_for_a_visible_answer():
    from tuber_studio.agent.tasks.title_form import grounded_options

    fact = "左侧有一盏红色台灯"
    async def call(messages, schema):
        packet = json.loads(messages[-1]["content"])
        if "frame_details" in packet:
            return {"fact":fact, "options":[{"evidence_span":"台灯","title":"这是谁的台灯？"},
                               {"evidence_span":"红色","title":"左侧的台灯是什么颜色？"}]}
        ownership = "谁" in packet["title_words"]
        return {"form":"问句","core":"谁" if ownership else "什么颜色","observation":fact,
                "supported":True,"answer":"" if ownership else "红色"}
    options = await grounded_options([fact], "问句", [], call)
    assert not options["candidates"][0]["reading"]["answer_bound"]
    assert options["chosen"] == "左侧的台灯是什么颜色？"


@pytest.mark.usefixtures("reviewed_window")
@pytest.mark.parametrize("reject", [False, True])
async def test_verified_title_goes_verbatim_to_the_final_image_review(runtime, store, monkeypatch, reject):
    from tuber_studio.agent.tasks import tools as task_tools
    from tuber_studio.agent.tasks.delivery import evidence_index
    from tuber_studio.agent.tasks.title_form import verified_title_fields

    read = short_read("read", 1)
    evidence = evidence_index([read])
    draft = selection(primary="read:1").model_dump()
    fact = "一人站在椅子旁"
    title = "椅子旁站着一个人。"
    fields = {"items.0.title":"使用核准语句"}
    inventory = {"read:1":{"labels":["座椅"], "details":[fact], "screen_text":[], "title_anchor":["座椅"]}}
    task = {"goal":"使用这一帧做提纲", "steps":[read], "verified_title_edits":{
                "items.0.title":{"primary_frame":"read:1", "original":draft["items"][0]["title"], "title":title, "fact":fact}},
            "plan":{"prior_goals":[], "candidate":draft, "editor_draft":draft, "editor_issues":fields, "frame_reviews":inventory}}
    with pytest.raises(DelegationError):
        verified_title_fields(task, draft, fields, inventory, set(fields))
    with pytest.raises(DelegationError):
        verified_title_fields(task, draft, fields, {}, set())
    audited = []
    async def image(*args):
        return {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,test"}}
    async def claims(texts, *args, **kwargs):
        assert texts["items.0.title"] == title
        return dict.fromkeys(texts, [])
    async def omni(messages, schema, **kwargs):
        props = schema["properties"]
        if "person_subject" in props:
            return {"person_subject": "地点、构图或物件"}
        if "alternatives" in props:
            return {"alternatives":{path:[] for path in json.loads(messages[-1]["content"])}}
        if props.get("items.0.title", {}).get("title") == "ObjectNames":
            assert json.loads(messages[-1]["content"])["items.0.title"] == title
            return {path:{"names":[]} for path in props}
        assert "items.0.title" not in props, "Already approved title was requested from the model again"
        if "requested_count" in props:
            return {"requested_count":1, "usage":"解释画面", "playable":True, "trace_origin":False}
        assert "field_basis" in props and title in str(messages[-1]["content"])
        audited.append(title)
        if reject:
            return {"supported":False, "issues":[{"field":"items.0.title", "quote":title, "reason":"引用未支持该断言"}]}
        return {"supported":True, "issues":[], "field_basis":{
            "items.0."+name:{"source":"read:1","observation":fact,"claim":"有来源的事实"} for name in ("title","rationale")}}
    monkeypatch.setattr(video_grounding, "clip_review_image", image)
    monkeypatch.setattr(video_grounding, "review_work_claims", claims)
    monkeypatch.setattr(runtime.llm, "generate_json", omni)
    monkeypatch.setattr(runtime.operation_llm, "generate_json", omni)
    editor = task_tools.create_task_runtime(runtime, store, None).edit_delivery
    if reject:
        with pytest.raises(DelegationError, match="核对未通过"):
            await editor(task, evidence)
        assert audited
        return
    result = await editor(task, evidence)
    assert audited and result.items[0].title == title
