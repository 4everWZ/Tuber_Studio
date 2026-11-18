from tuber_studio.agent.task_tools import review_work_claims


async def test_visible_description_is_not_a_work_target_even_when_both_extractors_copy_it():
    fields = {"title": "宫廷场景", "reason": "画面中央是一把木椅。", "named": "这是未署名作品中的一幕。",
              "quoted": "节目资料列有《例作》。", "known": "节目资料列有例作。", "episode": "素材来自该剧第二集。"}
    descriptors = {"宫廷场景", "画面中央是一把木椅"}
    targets = {"named": "未署名作品", "quoted": "例作", "known": "例作", "episode": "第二集"}

    async def omni(messages, schema, **kwargs):
        props = schema["properties"]
        if "type_word" in props:
            return {"type_word": ""}
        if all(v.get("enum") for v in props.values()):
            return {k: v["enum"][-1] for k, v in props.items()}
        if "work_claims" not in props:
            return {k: [{"span": targets.get(k, fields[k].rstrip("。")), "kind": "特定作品专名"}] for k in props}
        slots = props["work_claims"]["properties"]
        assert set(slots) == targets.keys()
        for k, slot in slots.items():
            assert slot["items"]["properties"]["target"]["enum"] == [targets[k]]
        return {"work_claims": {k: [{"phrase": fields[k], "target": target, "relation":
            "断言镜头或人物的作品或集归属" if k in {"episode", "named"} else "提及节目资料"}] for k, target in targets.items()}}

    claims = await review_work_claims(fields, omni, 1024, visible_descriptions=descriptors, known_names={"例作"})
    assert claims["title"] == claims["reason"] == []
    assert all(claims[k][0]["target"] == value for k, value in targets.items())
