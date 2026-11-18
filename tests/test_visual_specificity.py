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
