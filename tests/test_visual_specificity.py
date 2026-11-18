import json

import pytest

from tuber_studio.agent.visual_specificity import review_visual_specificity, shared_visual_facts


@pytest.mark.parametrize(
    ("verdict", "observation", "cue", "rejected"),
    [
        ("另一解释仍可能或更符合", "棕色表面上有深色条纹", "", True),
        ("原词仅描述共同可见特征", "棕色表面上有深色条纹", "", True),
        ("画面特征可排除另一解释", "表面呈线纹", "", True),
        ("画面特征可排除另一解释", "棕色表面上有深色条纹", "破口处露出从表面延续到横截面的木纤维", False),
        ("原词仅描述共同可见特征", "表面可见木纹", "", False),
        ("原词仅描述共同可见特征", "表面可见木纹", "仍需查看断面才能区分材料", False),
    ],
)
async def test_specific_name_requires_a_distinguishing_feature(verdict, observation, cue, rejected):
    term = "木纹" if observation == "表面可见木纹" else "实木"
    fields = {"title": f"{term}饰面", "reason": "用棕色的条纹过渡到下一镜。"}
    image = {"type": "image_url", "image_url": {"url": "fixture"}}

    async def model(messages, schema, **kwargs):
        if "alternatives" in schema["properties"]:
            assert json.loads(messages[-1]["content"]) == fields
            return {
                "alternatives": {
                    "title": [{"term": term, "other": "印花贴面", "discriminator": "可见横截面构造"}],
                    "reason": [],
                }
            }
        if "original_name" in schema["properties"]:
            return {"counterfactual_clause": fields["title"] + "（印花贴面）",
                    "original_name": "原称呼指定了另一解释不能保证的种类或属性" if rejected else "另一解释本身就是原称呼的一种",
                    "unsupported_modifiers": [], "basis": observation}
        assert messages[-1]["content"][1] == image
        return {
            "0": {
                "verdict": verdict,
                "observation": observation,
                "common_label": "木纹" if term == "木纹" else "深色条纹",
                "distinguishing_cue": cue,
                "identifying_feature": cue,
            }
        }

    rows = await review_visual_specificity(fields, image, "v:1", "棕色条纹", model, 1024)
    assert set(rows) == ({"title"} if rejected else set())
    if rejected:
        assert rows["title"]["text"] == fields["title"] and rows["title"]["primary_frame"] == "v:1"
        assert rows["title"]["claims"][0]["observation"] == observation


async def test_repair_offers_only_same_image_observations_that_survive_comparison():
    image = {"type": "image_url", "image_url": {"url": "fixture"}}
    common = "中央的棕色表面分布着间距不等的深色条纹。"

    async def model(messages, schema, **kwargs):
        props = schema["properties"]
        content = messages[-1]["content"]
        if "details" in props:
            assert content[-1] == image
            assert common in json.loads(content[0]["text"])["原帧观察"]
            return {"details": ["中央摆着一块复合板。", common, "原图未曾提及的新的器物", "实木"]}
        if "alternatives" in props:
            assert json.loads(content)["1"] == common
            return {
                "alternatives": {
                    "0": [{"term": "复合板", "other": "印花贴面", "discriminator": "分层断面"}],
                    "1": [],
                }
            }
        assert content[-1] == image
        return {
            "0": {
                "verdict": "另一解释仍可能或更符合",
                "observation": "只有正面可见。",
                "common_label": "饰面",
                "distinguishing_cue": "",
                "identifying_feature": "",
            }
        }

    assert await shared_visual_facts(
        image, "read:1", "原观察推测为实木。", [common, "中央摆着一块复合板。"], ["实木"], model, 1024
    ) == [common]


async def test_repair_reads_remaining_inventory_when_first_choice_is_disputed():
    inventory = ["中央放着瓷器。", "右侧有红色圆形标志。"]
    examined = []

    async def model(messages, schema, **kwargs):
        if "details" in schema["properties"]:
            return {"details": inventory[:1]}
        if "alternatives" in schema["properties"]:
            text = json.loads(messages[-1]["content"])["0"]
            examined.append(text)
            entries = [{"term": "瓷器", "other": "涂釉陶器", "discriminator": "胎体"}] if text == inventory[0] else []
            return {"alternatives": {"0": entries}}
        return {"0": {"verdict": "另一解释仍可能或更符合", "common_label": "带釉器皿",
                      "observation": "仅见表面", "distinguishing_cue": "", "identifying_feature": ""}}

    assert await shared_visual_facts({}, "v:1", "", inventory, ["实木"], model, 1024) == inventory[1:]
    assert examined == inventory


async def test_same_visual_assertion_binds_both_fields_and_preserves_editing_use():
    fields = {
        "items.1.title": "中央是实木饰面",
        "items.1.rationale": "画面依据：中央是实木饰面。剪辑安排：接在第一句介绍后。",
    }
    image = {"type": "image_url", "image_url": {"url": "fixture"}}

    async def model(messages, schema, **kwargs):
        if "alternatives" in schema["properties"]:
            assert set(json.loads(messages[-1]["content"]).values()) == {"中央是实木饰面"}
            return {
                "alternatives": {
                    "items.1.title": [{"term": "实木", "other": "印花饰面", "discriminator": "断面构造"}],
                    "items.1.rationale": [],
                }
            }
        return {
            "0": {
                "verdict": "另一解释仍可能或更符合",
                "observation": "表面有深浅条纹。",
                "common_label": "条纹",
                "distinguishing_cue": "",
                "identifying_feature": "",
            }
        }

    rows = await review_visual_specificity(fields, image, "read:1", "条纹表面", model, 1024)
    assert {k: r["text"] for k, r in rows.items()} == fields


async def test_ruling_out_one_alternative_does_not_identify_the_material():
    fields = {"title": "棕色实木饰面"}

    async def model(messages, schema, **kwargs):
        if "alternatives" in schema["properties"]:
            return {
                "alternatives": {"title": [{"term": "实木", "other": "镶嵌石材", "discriminator": "切面"}]}
            }
        return {
            "0": {
                "verdict": "画面特征可排除另一解释",
                "observation": "棕色条纹",
                "common_label": "条纹",
                "distinguishing_cue": "可见薄片弯曲",
                "identifying_feature": "",
            }
        }

    assert "title" in await review_visual_specificity(fields, {}, "read:1", "", model, 1024)


@pytest.mark.parametrize(
    ("assertion", "term", "other", "referent", "shared", "detail", "unsupported"),
    [
        ("画面中央是一位身穿华丽服饰的人物", "人物", "神像", "人物", "人形造型", "穿有带花纹的服装", ""),
        ("画面中央是一位头戴红色头盔的人物", "人物", "神像", "人物", "人形造型", "红色帽饰", "头盔"),
        ("座椅后方有金色雕花屏风", "屏风", "宫墙", "屏风", "龙纹装饰", "可见成排金色雕饰", "屏风"),
        ("画面右侧的人物戴着红色帽子", "红色帽子", "侍者", "人物", "身着古装的形象", "头上有红色帽饰", ""),
    ],
)
async def test_shared_appearance_checks_whole_original_clause(assertion, term, other, referent, shared, detail, unsupported):
    image = {"type": "image_url", "image_url": {"url": "bound-frame"}}
    fields = {"items.0.title": assertion, "items.0.rationale": f"画面依据：{assertion}。剪辑安排：用于开场。"}
    checks = []

    async def model(messages, schema, **kwargs):
        props = schema["properties"]
        if "alternatives" in props:
            return {"alternatives": {
                "items.0.title": [{"term": term, "other": other, "discriminator": "类别构造"}],
                "items.0.rationale": [],
            }}
        content = messages[-1]["content"]
        if "original_name" in props:
            assert content[1] is image
            packet = json.loads(content[0]["text"])
            assert packet["原图所见"] == detail and packet["假定另一解释为真"] == other
            checks.append(packet["整句原文"])
            relation = "原称呼指画中被表现的形象，另一解释是其载体"
            if unsupported == term:
                relation = "原称呼指定了另一解释不能保证的种类或属性"
            elif referent != term:
                relation = "另一解释本身就是原称呼的一种"
            hypothetical = assertion.replace(referent, other) if referent != term or unsupported == term else assertion + f"（{other}）"
            return {"counterfactual_clause": hypothetical, "original_name": relation,
                    "unsupported_modifiers": [unsupported] if unsupported and unsupported != term else [],
                    "basis": detail}
        assert content[1] is image
        return {"0": {"verdict": "原词仅描述共同可见特征", "common_label": shared,
                      "observation": detail, "distinguishing_cue": "", "identifying_feature": ""}}

    rows = await review_visual_specificity(fields, image, "read:1", detail, model, 1024)
    assert checks == [assertion]
    assert set(rows) == (set(fields) if unsupported else set())
    for row in rows.values():
        claim, = row["claims"]
        assert claim["term"] == claim["target"] == claim["phrase"] == unsupported


@pytest.mark.parametrize("terms", [("红色帽子", "华丽"), ("华丽", "红色帽子")])
async def test_same_alternative_is_bound_to_the_person_in_both_objections(terms):
    assertion = "画面右侧站立一名身穿华丽传统服饰的人物，头戴红色帽子"
    fields = {"title": assertion}
    image = {"type": "image_url", "image_url": {"url": "bound-frame"}}

    async def model(messages, schema, **kwargs):
        props = schema["properties"]
        if "alternatives" in props:
            return {"alternatives": {"title": [
                {"term": t, "other": "侍者", "discriminator": "身份标记"} for t in terms
            ]}}
        assert messages[-1]["content"][1] is image
        if "counterfactual_clause" in props:
            term = json.loads(messages[-1]["content"][0]["text"])["原称呼"]
            hypothetical = assertion.replace("人物", "侍者") if term == "华丽" else assertion.replace("红色帽子", "侍者帽")
            return {"counterfactual_clause": hypothetical, "original_name": "原称呼指定了另一解释不能保证的种类或属性",
                    "unsupported_modifiers": [], "basis": "推测人物身份"}
        return {key: {"verdict": "原词仅描述共同可见特征", "common_label": "身穿传统服饰的人物",
                      "observation": "右侧有人站立", "distinguishing_cue": "", "identifying_feature": ""}
                for key in props}

    assert await review_visual_specificity(fields, image, "read:1", "", model, 1024) == {}
