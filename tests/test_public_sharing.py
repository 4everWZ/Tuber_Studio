import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from test_dynamics import service as service

from tuber_studio.infra.platforms.bilibili.browsing import BrowsingAdapter
from tuber_studio.infra.platforms.bilibili.client import PublishUncertain
from tuber_studio.infra.platforms.bilibili.dynamics import publish_image, publish_text
from tuber_studio.operations.channels.comments.timeline import observe_thread
from tuber_studio.operations.channels.dynamics import publisher as dynamics_module
from tuber_studio.operations.channels.mentions import bind_mentions

TEXT = "🌊 和 @阿蓝 一起看日出，再叫上 @贝贝 。"
PEOPLE = [{"uid": "71", "name": "阿蓝"}, {"uid": "72", "name": "贝贝"}]
REQUESTS = [{"label": "@" + item["name"], "account": item["uid"]} for item in PEOPLE]


@pytest.mark.parametrize("kind", ["text", "image", "primary", "video_reply", "dynamic_reply", "plain"])
async def test_mentions_use_platform_fields_and_verify_original(kind, monkeypatch):
    adapter = BrowsingAdapter({"sessdata": "fixture", "csrf": "fixture"})
    await adapter.client.aclose()
    mentions, pending = await bind_mentions(adapter, TEXT, REQUESTS, PEOPLE)
    assert len(mentions) == 2 and not pending
    posts = []
    target = {"bvid": "BV1d8pU6xEZX", "oid": "12", "resource_type": "video", "type": 1,
              "root": "0" if kind == "primary" else "41", "parent": "0" if kind == "primary" else "42",
              "mentions": mentions}
    if kind == "dynamic_reply":
        target.update(resource_type="dynamic", type=11)
    picture = {"url": "https://i0.hdslb.com/bfs/fake.png", "width": 64, "height": 64}

    async def upload(*args, **kwargs):
        return picture

    monkeypatch.setattr("tuber_studio.infra.platforms.bilibili.dynamics.upload_image", upload)

    def respond(request):
        path = request.url.path
        if path.endswith("/nav"):
            data = {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                                "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"}}
        elif path.endswith("/dyn"):
            contents = json.loads(request.content)["dyn_req"]["content"]["contents"]
            posts.append(contents)
            data = {"dyn_id_str": "502"}
        elif path.endswith("/add"):
            posts.append(parse_qs(request.content.decode()))
            data = {"rpid": "503"}
        elif path.endswith("/detail"):
            nodes = [{"type": "RICH_TEXT_NODE_TYPE_AT" if item["type"] == 2 else "RICH_TEXT_NODE_TYPE_TEXT",
                      "text": item["raw_text"], "rid": item["biz_id"]} for item in posts[0]]
            data = {"item": {"id_str": "502", "type": "DYNAMIC_TYPE_DRAW" if kind == "image" else "DYNAMIC_TYPE_WORD",
                             "modules": {"module_author": {"mid": "900"}, "module_dynamic": {
                                 "desc": {"text": TEXT, "rich_text_nodes": nodes}, "major": {
                                     "draw": {"items": [{"src": picture["url"], "width": 64, "height": 64}]}
                                 },
                             }}}}
        else:
            assert path in {"/x/v2/reply", "/x/v2/reply/reply"}
            data = {"replies": [{"rpid": 503, "root": int(target["root"]), "parent": int(target["parent"]),
                                 "member": {"mid": 900}, "content": {"message": TEXT,
                                 "members": [{"mid": item["uid"], "uname": item["name"]} for item in PEOPLE]}}]}
        return httpx.Response(200, json={"code": 0, "data": data})

    adapter.client = httpx.AsyncClient(base_url="https://api.bilibili.com", transport=httpx.MockTransport(respond))
    async with adapter.client:
        if kind == "plain":
            await publish_text(adapter, TEXT.replace("@", ""))
            assert posts[0] == [{"raw_text": TEXT.replace("@", ""), "biz_id": "", "type": 1}]
            return
        if kind in {"text", "image"}:
            if kind == "image":
                receipt = await publish_image(adapter, TEXT, {"content": b"isolated"}, mentions=mentions)
            else:
                receipt = await publish_text(adapter, TEXT, mentions=mentions)
            result = await adapter.reconcile_dynamic_publication({"mentions": mentions, "message_kind": kind,
                "images": [{"path": "/bfs/fake.png", "width": 64, "height": 64}]},
                author_uid="900", text=TEXT, started_at=time.time(), receipt=receipt)
            assert [item["biz_id"] for item in posts[0] if item["type"] == 2] == ["71", "72"]
        else:
            publish = adapter.publish_top_level if kind == "primary" else adapter.publish_resource_reply
            receipt = await publish(target, TEXT)
            assert json.loads(posts[0]["at_name_to_mid"][0]) == {"阿蓝": 71, "贝贝": 72}
            assert posts[0]["parent"] == [target["parent"]]
            result = await adapter.reconcile_publication(target, author_uid="900", text=TEXT, started_at=time.time(), receipt=receipt)
        assert len(posts) == 1
        assert result["status"] == "visible" and result["receipt"]["mention_uids"] == ["71", "72"]


async def test_same_name_requires_selection_and_new_target_requires_review(service):
    people = PEOPLE + [{"uid": "81", "name": "阿蓝"}]
    query = [{"label": "@阿蓝", "account": "阿蓝"}]
    bound, pending = await bind_mentions(object(), TEXT, query, people)
    assert not bound and [item["uid"] for item in pending[0]["candidates"]] == ["71", "81"]
    mentions, pending = await bind_mentions(object(), TEXT, [REQUESTS[0]], people)
    assert not pending and mentions[0]["uid"] == "71"
    payload = {"action": "post", "reason": "感想", "source_ids": [], "content": TEXT, "next_evaluation_seconds": 1800,
               "presentation": {"link_source_ids": [], "mentions": mentions, "pending_mentions": []}}
    post_id = service._save_decision(service._character(), True, payload, [])["post_id"]
    reviewed = service.review(post_id, "approve")
    new_mentions, _ = await bind_mentions(object(), TEXT, [{"label": "@阿蓝", "account": "81"}], people)
    edited = service.review(post_id, "edit", mention_selection={"mentions": new_mentions, "pending_mentions": []})
    assert edited["status"] == "pending_review" and edited["review_key"] != reviewed["review_key"]
    with pytest.raises(ValueError, match="草稿已变化"):
        service.review(post_id, "approve", review_key=reviewed["review_key"])


async def test_shared_update_history_includes_pending_confirmed_and_new_comment(service):
    from types import SimpleNamespace

    from tuber_studio.operations.browsing.repository import BrowsingRepository

    BrowsingRepository(service.store)
    service.account.display_account_id = lambda: "900"
    service.account.account_id = fake_account
    service.runtime.browsing = SimpleNamespace(account="900", policy=lambda: {}, sends=SimpleNamespace(withdraw=lambda *args: None))
    character = service._character()
    bvid = "BV1d8pU6xEZX"
    context = {"id": "operation", "account_id": "900", "character_id": character["id"]}
    observe_thread(service.store, context, bvid, [
        {"comment_id": "41", "user_id": "71", "display_name": "阿蓝", "text": "灯光像日出"},
        {"comment_id": "42", "user_id": "72", "display_name": "贝贝", "text": "画面就像天亮了"},
    ], via="comment_visit")
    sources = service._sources()
    ids = [source["id"] for source in sources]
    post = service._save_decision(character, True, {
        "action": "post", "reason": "同一件事", "content": "蓝转暖的一秒像日出。", "source_ids": ids,
        "next_evaluation_seconds": 1800, "presentation": {"points": [{"text": "画面像日出", "source_ids": ids}]},
    }, sources)
    observe_thread(service.store, context, bvid, [
        {"comment_id": "43", "user_id": "73", "display_name": "珊瑚", "text": "UP更新了另一版，这次还有潮声"},
    ], via="comment_visit")
    update = [source for source in service._sources() if source["id"].endswith(":43")]
    history, = service._related_posts(character, "900", update)
    assert history["status"] == "pending" and history["points"][0]["source_ids"] == ids
    service._status(post["post_id"], "published", receipt={"dynamic_id": "133"})
    history, = service._related_posts(character, "900", update)
    assert history["status"] == "shared"
    assert service._related_posts(character, "901", update) == []


async def fake_account():
    return "900"


async def test_video_with_visual_evidence_can_be_shared_twice(service):
    from types import SimpleNamespace

    from tuber_studio.operations.browsing.repository import BrowsingRepository

    BrowsingRepository(service.store)
    service.account.display_account_id = lambda: "900"
    service.account.account_id = fake_account
    service.runtime.browsing = SimpleNamespace(
        account="900", policy=lambda: {}, sends=SimpleNamespace(withdraw=lambda *args: None),
    )
    char = service._character()
    service.operations.configure({"enabled": True}, account_id="900", character=char)
    await service.set_policy({"max_posts_per_day": 0, "min_post_interval_seconds": 0})
    video = "BV1d8pU6xEZX"
    material = {"metadata": {"bvid": video, "title": "从蓝色到日出", "owner_uid": "72", "owner": "贝贝"},
                "parts": [{"cid": "6", "visual": True, "segments": [{"start": 12.5, "text": "一道橙光落到窗台"}]}]}
    service.store.execute(
        "INSERT INTO browsing_video_progress(account_id,character_id,bvid,material,decisions,updated_at,read_state) "
        "VALUES(?,?,?,?,'[]',?,'viewed')", ("900", char["id"], video, json.dumps(material), time.time()),
    )
    source, = service._sources()
    assert source["participants"] == [PEOPLE[1]] and "12.5s · 画面" in source["text"]
    for text in ("那道橙光好像给画面换了季节。", "过几天想起那道光落在窗台上，像给小小一间屋子开了场日出。"):
        service.runtime.operation_llm.output = json.dumps({
            "action": "post", "reason": "想分享一处观影细节", "source_ids": [source["id"]],
            "content": text, "next_evaluation_seconds": 1800,
        })
        decision = await service.evaluate(manual=True)
        service.review(decision["post_id"], "approve")
        assert (await service.publish(decision["post_id"]))["status"] == "published"
    assert len(service.sent) == 2 and len(service.store.all("SELECT * FROM account_writes")) == 2


async def test_mention_timeout_reconciles_original_once_after_service_restart(service, monkeypatch):
    from types import SimpleNamespace

    from tuber_studio.core.errors import UnknownPublication
    from tuber_studio.operations.channels.dynamics import Dynamics

    service.account.account_id = fake_account
    service.account.display_account_id = lambda: "900"
    service.operations.configure({"enabled": True}, account_id="900", character=service._character())
    platform = {"posts": [], "visible": False}
    mentions, _ = await bind_mentions(object(), TEXT, REQUESTS, PEOPLE)

    async def submit(adapter, text, *, before_send, mentions):
        before_send()
        platform["posts"].append((text, mentions))
        raise PublishUncertain("isolated connection loss after write")

    async def read(target, *, author_uid, text, started_at, receipt):
        assert author_uid == "900" and [(text, target["mentions"])] == platform["posts"]
        return {"status": "visible", "receipt": {"dynamic_id": "625", "mention_uids": ["71", "72"]}} if platform["visible"] else {
            "status": "not_found"
        }

    service.account.adapter = SimpleNamespace(reconcile_dynamic_publication=read)
    monkeypatch.setattr(dynamics_module, "publish_text", submit)
    post = service._save_decision(service._character(), True, {
        "action": "post", "reason": "约大家一起看日出", "source_ids": [], "content": TEXT,
        "next_evaluation_seconds": 1800, "presentation": {"mentions": mentions, "link_source_ids": []},
    }, [])["post_id"]
    service.review(post, "approve")
    with pytest.raises(UnknownPublication):
        await service.publish(post)
    assert service._post(post)["status"] == "publish_uncertain" and len(platform["posts"]) == 1
    resumed = Dynamics(service.settings, service.store, service.runtime, service.comments)
    platform["visible"] = True
    assert (await resumed.publish(post))["receipt"]["dynamic_id"] == "625"
    assert (await resumed.publish(post))["status"] == "published" and len(platform["posts"]) == 1
