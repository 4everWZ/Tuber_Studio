import asyncio
import json
from types import SimpleNamespace

import pytest

from tuber_studio.agent.tasks.tools import (
    attribution_receipt,
    cited_programme_names,
    requested_title_form,
    selected_title,
    unsupported_timecode,
)
from tuber_studio.agent.tasks.visual_identity import check_people, check_screen_title, extract_people
from tuber_studio.core.identity import (
    captured_identity,
    provider_identity_messages,
    visual_editor_context,
)


def test_every_occurrence_of_a_named_editor_needs_its_own_relation():
    editor = "小荷"
    credits = [{"person": editor, "relation": "投稿署名", "source": "post"}]
    text = "小荷的宫廷场景，投稿署名小荷"
    receipt = check_people("items.0.title", text, [
        {"phrase": "投稿署名小荷", "person": editor, "relation": "投稿署名"}],
        primary="v:1", editor=editor, credits=credits, screen_text=[])
    assert receipt["claims"][0]["sources"] == credits
    assert not receipt["claims"][0]["issue"]
    assert receipt["claims"][1]["offset"] == 0 and receipt["claims"][1]["issue"]


@pytest.mark.parametrize(("relation", "supported"), [
    ("画面文字提名", True), ("节目导演", True), ("画中人物身份", False), ("场景或物件归属", False),
])
def test_screen_text_and_programme_credits_do_not_identify_the_people_in_the_frame(relation, supported):
    ref = {"person": "叶甲", "phrase": "叶甲", "relation": relation}
    receipt = check_people("items.0.title", "叶甲", [ref], primary="v:3", editor="编辑乙",
                           credits=[{"person":"叶甲", "relation":"节目导演", "source":"page"}],
                           screen_text=["导演 叶甲"])
    assert (receipt["claims"][0]["issue"] is None) == supported


def test_legacy_programme_title_and_literal_poster_ocr_have_different_scope():
    page = {"id":"official", "tool":"web_page", "result":{
        "url":"https://tv.cctv.com/2012/12/03/VIDA100.shtml", "title":"春江_CCTV节目官网-电视剧_央视网(cctv.com)",
        "excerpt":"节目介绍"}}
    item = SimpleNamespace(sources=["official"], primary_frame="v:1")
    assert cited_programme_names([page], item.sources) == {"春江"}
    def audit(text, relation, *, kind="", ocr=(), pages=()):
        return attribution_receipt("items.0.rationale", text, [{
            "phrase":text, "target":"春江", "relation":relation, "work_type":kind}], pages, item, screen_text=ocr)["claims"][0]
    assert not audit("海报上写着《春江》", "转述画面可读题名", ocr=["春江"])["issue"]
    assert audit("海报上写着《春江》", "转述画面可读题名", pages=[page])["issue"]
    bad = audit("电影《春江》的海报", "转述画面可读题名", kind="电影", ocr=["春江"], pages=[page])
    assert bad["issue"] and bad["catalogue_types"] == ["电视剧"] and bad["claimed_types"] == ["电影"]
    assert not audit("节目资料列有电视剧《春江》", "提及节目资料", kind="电视剧", pages=[page])["issue"]
    assert audit("这是《春江》的一幕", "断言镜头或人物的作品或集归属", pages=[page], ocr=["春江"])["issue"]


async def test_only_the_visual_edit_loses_runtime_identity_during_overlapping_calls():
    provider = SimpleNamespace(identity_resolver=lambda: {"identity_version":"test", "character_name":"编辑"})
    messages = [{"role":"system", "content":"核对"}, {"role":"user", "content":"画面"}]
    async def independently_bound():
        await asyncio.sleep(0)
        return captured_identity(provider_identity_messages(provider, messages))
    chat = asyncio.create_task(independently_bound())
    with visual_editor_context():
        assert await independently_bound() is None
        assert provider_identity_messages(provider, messages) == messages
    assert (await chat)["character_name"] == "编辑"
    assert (await independently_bound())["character_name"] == "编辑"


def test_editable_title_can_use_verbatim_fact_or_compose_new_surface_text():
    facts = ["窗旁一人"]
    assert selected_title({"use_fact":"窗旁一人"}, facts) == facts[0]
    assert selected_title("窗边站着谁？", facts) == "窗边站着谁？"
    with pytest.raises(ValueError):
        selected_title({"use_fact":"编辑在窗旁"}, facts)


async def test_names_absent_from_the_field_cannot_be_cited_as_a_field_defect():
    fields = {"title": "宫殿内部", "alternate": "故宫金雕宝座"}
    async def omni(messages, schema, **kwargs):
        text = messages[-1]["content"]
        assert text in fields.values() and "频道甲" not in str(messages) and "image_url" not in str(messages)
        if "person_subject" in schema["properties"]:
            return {"person_subject": schema["properties"]["person_subject"]["enum"][-1]}
        return {"mentions":[{"span":"频道甲","kind":"人名、专属昵称、明确指向特定人的名号"}]}
    parsed = await extract_people(fields, omni, 512)
    for path, text in fields.items():
        receipt = check_people(path, text, parsed[path], editor="编辑", primary="v:1",
                               credits=[], screen_text=["频道甲 bilibili"])
        assert not receipt["claims"]


async def test_apparel_description_with_and_without_a_personal_name():
    description = "穿着华丽服饰、头戴红色头盔的人物"
    fields = {"a":description,"b":"穿蓝衣的演员林甲"}
    async def omni(messages,schema,**kwargs):
        props = schema["properties"]
        text = messages[-1]["content"]
        if "person_subject" in props:
            return {"person_subject":props["person_subject"]["enum"][-1]}
        if "mentions" in props:
            return {"mentions":[{"span":description,"kind":"服饰、位置、动作等外观指称"}] if text == description else
                               [{"span":"林甲","kind":"人名、专属昵称、明确指向特定人的名号"}]}
        assert props["person_references"]["items"]["properties"]["person"]["enum"] == ["林甲"]
        return {"person_references":[{"phrase":text,"person":"林甲","relation":"画中人物身份"}]}
    result = await extract_people(fields,omni,512)
    assert not result["a"]
    assert check_people("b",fields["b"],result["b"],editor="编辑",primary="v:1",credits=[],screen_text=[])["claims"][0]["issue"]


def test_written_part_and_time_must_belong_to_this_clip():
    point = SimpleNamespace(part=2,seconds=.84)
    clip = {"start":0,"end":5.8}
    assert unsupported_timecode("P2 0:37 金色字体",point,clip) == "P2 0:37"
    assert unsupported_timecode("P3 00:02",point,clip)
    assert not unsupported_timecode("P2 0:00 金色字体",point,clip)
    assert not unsupported_timecode("屏上计时 0:37",point,clip)


async def test_requested_form_needs_a_literal_owner_instruction_about_form():
    calls = []
    async def assess(messages, schema, **kw):
        text = messages[-1]["content"]
        calls.append(text)
        return {"explicit_form":"问句" if text == "标题用问句" else "未规定标题句式"}
    inputs = ["读两段素材；标题用问句"]
    for quote in ("","没有写过","读两段素材"):
        assert await requested_title_form({"requested_form":"问句","form_quote":quote}, inputs, assess, 512) is None
    assert await requested_title_form({"requested_form":"问句","form_quote":"标题用问句"}, inputs, assess, 512) == "问句"
    assert calls == ["读两段素材","标题用问句"]


@pytest.mark.parametrize(("fragment","mode","supported"), [
    ("青山", "以屏字命名主体或作品", False),
    ("青山", "把字形或残字解释为节目内容主题", False),
    ("青山", "明确转录当前可见的字", True),
    ("青山影记", "以屏字命名主体或作品", True),
])
async def test_animated_mark_transcript_does_not_authorise_a_new_subject(fragment, mode, supported):
    async def audit(messages, schema, **kwargs):
        if "title_subject" in schema["properties"]:
            return {"title_subject":"具体物件、字形、构图或视觉载体","subject_words":""}
        assert messages[-1]["content"][1]["type"] == "image_url"
        return {fragment:mode}
    result = await check_screen_title("items.0.title", f"{fragment}，金色字体", "v:1", {"type":"image_url"},
        [fragment], [{"person":"青山影记","relation":"投稿署名"}], set(), audit, 512)
    assert (result["claims"][0]["issue"] is None) == supported


async def test_theme_inferred_from_typeface_is_checked_even_without_copying_any_letters():
    async def audit(messages, schema, **kwargs):
        if "title_subject" in schema["properties"]:
            assert messages[-1]["content"] == "书法艺术"
            return {"title_subject":"以内容主题、学科或艺术门类命名","subject_words":"书法艺术"}
        return {"topic_evidence":"只能看到字形、图形设计或局部字样"}
    result = await check_screen_title("items.0.title","书法艺术","v:1",{"type":"image_url"},["甲乙影像"],[],set(),audit,512)
    assert result["claims"][0]["phrase"] == "书法艺术" and result["claims"][0]["issue"]


@pytest.mark.parametrize("supported", [True, False])
async def test_unbound_reader_excerpt_checks_the_actual_full_title_against_the_frame(supported):
    title = "金色的雕饰"
    async def audit(messages, schema, **kwargs):
        if "title_subject" in schema["properties"]:
            return {"title_subject":"以内容主题、学科或艺术门类命名", "subject_words":"工艺史"}
        content = messages[-1]["content"]
        assert json.loads(content[0]["text"]) == {"候选主题": title}
        assert content[1]["type"] == "image_url"
        return {"topic_evidence":"主帧直接展示该主题的内容或实践" if supported else "内容无法辨明"}
    result = await check_screen_title("items.0.title", title, "v:1", {"type":"image_url"},
                                      ["栏目"], [], set(), audit, 512)
    claim, = result["claims"]
    assert claim["phrase"] == title and claim["reader_excerpt"] == "工艺史"
    assert (claim["issue"] is None) == supported
