import asyncio
import json
import os
import socket
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest

from evaluations import creative_memory_browser as runner
from evaluations import creative_memory_receipt as evidence
from evaluations.creative_browser_receipt import Mismatch, load
from evaluations.creative_memory_receipt import check_citations, title_changes


@pytest.mark.parametrize(
    "origin",
    [
        "https://192.0.2.1",
        "https://192.0.2.1:443",
        "https://192.0.2.1:9443",
        "http://192.0.2.1:9444",
        "https://127.0.0.1:9444",
    ],
)
def test_browser_excludes_migration_preflight_and_local_origins(origin):
    with pytest.raises(Mismatch):
        runner.workstation_origin(origin)
    assert runner.workstation_origin("https://192.0.2.1:9444/") == "https://192.0.2.1:9444"


def test_workstation_checkpoint_requires_fresh_directory_session_and_no_platform_posts(tmp_path, monkeypatch):
    args = SimpleNamespace(runtime=tmp_path / "run", bind="127.0.0.1", port=9000)
    monkeypatch.setenv("OMNI_GATEWAY_URL", "https://model.invalid")
    monkeypatch.setenv("OMNI_STUDIO_TOKEN", "fixture-key")
    monkeypatch.delenv("OMNI_CORE_TOKEN_FILE", raising=False)

    async def offline(self, request):
        return httpx.Response(503, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline)

    async def inspect(app):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client,
        ):
            assert (await client.get(runner.CHECKPOINT + "/identity")).json()["run"] == "run"
            assert (await client.get(runner.CHECKPOINT)).status_code == 401
            login = (args.runtime / "admin-token").read_text().strip()
            assert (await client.post("/api/auth", json={"token": login})).status_code == 200
            assert (await client.get(runner.CHECKPOINT)).json() == {
                "run": "run",
                "denied": 0,
                "platform": dict.fromkeys(runner.WRITE_TABLES, 0),
            }

            async with httpx.AsyncClient(trust_env=False) as http:
                assert (
                    await http.post("https://model.invalid/v1/browser-tickets", json={"path": "/v1/realtime"})
                ).status_code == 503
                with pytest.raises(Mismatch, match="拒绝对外写入"):
                    await http.post("https://model.invalid/v1/browser-tickets", json={"path": "/v1/other"})
                with pytest.raises(Mismatch, match="拒绝对外写入"):
                    await http.post("https://platform.invalid/publish")
            assert (await client.get(runner.CHECKPOINT)).json()["denied"] == 2

    with (
        patch.dict("os.environ"),
        patch.object(runner.uvicorn, "run", lambda app, **_: asyncio.run(inspect(app))),
    ):
        with pytest.raises(Mismatch, match="拒绝对外写入"):
            runner.serve(args)
        with pytest.raises(FileExistsError):
            runner.serve(args)
    receipt = load(args.runtime / "p2-host.json")
    assert not any(receipt["baseline"].values())
    assert receipt["denied"] == 2 and not any(receipt["platform"].values())
    assert "fixture-key" not in str(receipt)


def memories():
    def row(identity, version, text, turn):
        return {"id": identity, "revision": version, "source_key": "turn:" + turn, "quote": text}

    return {
        "layout": row(1, 1, runner.CONFIRMED, "format"),
        "original": row(2, 1, runner.AUTO, "first"),
        "corrected": row(2, 2, runner.VOICE_RULE, "second"),
    }


async def test_blocked_first_edition_keeps_voice_failure_separate_and_preserves_preference(tmp_path):
    scenario = runner.MemoryScenario("https://192.0.2.1:9444", tmp_path, None)
    scenario.report = memories()
    page = object()
    with (
        patch.object(scenario, "current", new_callable=AsyncMock),
        patch.object(
            scenario, "correct_voice", new_callable=AsyncMock, side_effect=Mismatch("unsigned")
        ) as voice,
        patch.object(scenario, "forget_title", new_callable=AsyncMock) as forget,
    ):
        await scenario.voice_after_blocked_edition(page, {"id": "task", "status": "needs_input"})
        voice.assert_awaited_once_with(page)
        forget.assert_not_awaited()
    assert scenario.report["creation_blocked"] == {"id": "task", "revision": 1, "status": "needs_input"}
    assert scenario.report["independent_voice_failure"] == {"error": "Mismatch"}


@pytest.mark.parametrize(
    "phase,labels", [("r1", ("layout", "original")), ("r2", ("layout", "corrected")), ("r3", ("layout",))]
)
def test_evidence_requires_matching_editor_version_quote_and_visible_source(phase, labels):
    memory = memories()
    wire = [memory[key] for key in labels]
    memory[phase + "_api"] = wire
    memory[phase + "_visible"] = [
        {"quote": r["quote"], "source": f"来源 {r['source_key']} · v{r['revision']}"} for r in wire
    ]
    check_citations(memory, phase, wire, wire)
    with pytest.raises(Mismatch):
        check_citations(memory, phase, wire, [])
    memory[phase + "_visible"][0]["source"] = "另一条原话"
    with pytest.raises(Mismatch, match="来源"):
        check_citations(memory, phase, wire, wire)


@pytest.mark.parametrize("phase,wrong", [("r2", "original"), ("r3", "corrected")])
def test_agreeing_but_stale_citations_fail(phase, wrong):
    memory = memories()
    stale = [memory["layout"], memory[wrong]]
    memory[phase + "_api"] = stale
    memory[phase + "_visible"] = [
        {"quote": r["quote"], "source": f"来源 {r['source_key']} · v{r['revision']}"} for r in stale
    ]
    with pytest.raises(Mismatch, match="有效的条目"):
        check_citations(memory, phase, stale, stale)


def test_title_comparison_targets_new_second_item_and_preserves_first():
    first = {"title": "灯在何处？", "start": 0, "end": 3}
    editions = {
        phase: {"items": [first, {"title": title, "start": start, "end": start + 3}]}
        for phase, title, start in (
            ("r1", "谁戴着帽子？", 10),
            ("r2", "帽顶为红色。", 20),
            ("r3", "金色座椅", 30),
        )
    }
    assert title_changes(editions)["r3"] == ["灯在何处？", "金色座椅"]
    editions["r2"]["items"][1]["title"] = "帽顶是什么颜色？"
    with pytest.raises(Mismatch, match="句号"):
        title_changes(editions)


@pytest.mark.parametrize("field", ["quote", "history_path", "source_key", "revision", "visible_revision"])
def test_settings_receipt_binds_visible_quote_and_revision_to_the_inspected_source(field):
    memory = memories()
    row = memory["layout"] | {"note": "原话摘要"}
    seen = {"quote": row["quote"], "history_path": "/api/daily-memory/organized/1/revisions",
            "source_key": row["source_key"], "revision": 1, "visible_revision": "v1 · 原话摘要"}
    memory.update(layout=row, citation_surface="settings", r3_api=[row], r3_visible=[seen])
    check_citations(memory, "r3", [row], [row])
    seen[field] = "另一版本"
    with pytest.raises(Mismatch, match="设置原话、来源或可见版本"):
        check_citations(memory, "r3", [row], [row])


def test_offline_memory_receipt_reads_sources_and_withdrawal_from_workstation_db(tmp_path, monkeypatch):
    # Isolate memory provenance here; the existing P2 verifier owns media decoding and chat navigation.
    browser = tmp_path / "browser"
    browser.mkdir()
    memory = memories()
    memory.update(run=tmp_path.name, withdrawn={"id": 2, "deleted": True}, withdrawal_turn="withdraw")
    corrected = memory["corrected"]
    corrected["source_key"] = "confirmed_voice:example"
    corrected["voice_origin"] = {
        "machine_text": "好的，" + runner.VOICE_RULE,
        "samples": 32000,
        "capture_id": "selected",
        "owner_edited": True,
    }
    memory["voice"] = {
        "status": "observed",
        "protocol": "explicit_user_audio",
        "signed_final_received": True,
        "confirmation_dialog_accepted": True,
        "confirm_http_status": 200,
        "capture_id": "selected",
        "explicit_samples": 32000,
        "bound_final_transcript": "好的，" + runner.VOICE_RULE,
        "owner_confirmed_text": runner.VOICE_RULE,
        "correction": {"id": 2, "revision": 2},
    }
    voice_db = {
        "id": 2,
        "revision": 2,
        "source_key": corrected["source_key"],
        "machine_text": "好的，" + runner.VOICE_RULE,
        "confirmed_text": runner.VOICE_RULE,
        "capture_id": "selected",
        "samples": 32000,
        "signature_verified": True,
        "source_bound": True,
    }
    for invalid in (
        {"signature_verified": False},
        {"source_bound": False},
        {"machine_text": runner.VOICE_RULE},
    ):
        with pytest.raises(Mismatch):
            evidence.check_voice(memory, voice_db | invalid)
    runner.write(tmp_path / "p2-voice-source.json", voice_db)
    memory["r2_voice_source_visible"] = (
        "机器转写：好的，" + runner.VOICE_RULE + "\n号主确认：" + runner.VOICE_RULE
    )
    first = {"title": "灯在何处？", "start": 0, "end": 3}
    editions = {}
    with closing(sqlite3.connect(tmp_path / "studio.sqlite3")) as db, db:
        db.executescript("""
            CREATE TABLE operator_chat_turns(request_id TEXT,message TEXT,result TEXT);
            CREATE TABLE agent_tasks(id TEXT,message TEXT,steps TEXT);
            CREATE TABLE creative_editions(task_id TEXT,task_revision INTEGER,edition INTEGER,body TEXT,author TEXT);
            CREATE TABLE daily_highlights(id INTEGER,revision INTEGER,quote TEXT,owner_principal TEXT,character_id TEXT);
            CREATE TABLE memory_source_tombstones(owner TEXT,character TEXT,source_key TEXT);
            CREATE TABLE confirmed_voice_origins(item_id INTEGER);
        """)
        for table in runner.WRITE_TABLES:
            db.execute(f"CREATE TABLE {table}(id INTEGER)")
        baseline = runner.counts(
            db,
            (
                "operator_chat_turns",
                "agent_tasks",
                "creative_editions",
                "daily_highlights",
                "memory_source_tombstones",
                *runner.WRITE_TABLES,
            ),
        )
        runner.write(
            tmp_path / "p2-host.json",
            {
                "run": tmp_path.name,
                "baseline": baseline,
                "denied": 0,
                "platform": runner.counts(db, runner.WRITE_TABLES),
                "inference": {"/v1/chat/completions": 3, "/llm/v1/responses": 3, "/v1/browser-tickets": 1},
            },
        )
        for key in ("layout", "original", "corrected"):
            row = memory[key]
            if key != "corrected":
                turn = row["source_key"][5:]
                memory[key + "_turn"] = turn
                db.execute(
                    "INSERT INTO operator_chat_turns VALUES(?,?,?)",
                    (turn, row["quote"], json.dumps({"memory_intent": "chat"})),
                )
            if key != "layout":
                db.execute(
                    "INSERT INTO memory_source_tombstones VALUES('owner','role',?)", (row["source_key"],)
                )
        db.execute("INSERT INTO daily_highlights VALUES(1,1,?,'owner','role')", (runner.CONFIRMED,))
        db.execute(
            "INSERT INTO operator_chat_turns VALUES('withdraw',?,?)",
            (evidence.REPLACE, json.dumps({"memory_intent": "steer_task", "card": {"data": {"id": "task"}}})),
        )
        for rev, title in enumerate(("帽子在哪？", "帽顶是红色。", "金色座椅"), 1):
            phase = f"r{rev}"
            citations = [memory["layout"]]
            if rev < 3:
                citations.append(memory["original" if rev == 1 else "corrected"])
            memory[phase + "_api"] = citations
            memory[phase + "_visible"] = [
                {"quote": r["quote"], "source": f"来源 {r['source_key']} · v{r['revision']}"}
                for r in citations
            ]
            body = "**保留段**\n\n**" + title + "**"
            artifact = {
                "task_id": "task",
                "task_revision": rev,
                "edition": 1,
                "body": body,
                "author": "agent",
            }
            db.execute(
                "INSERT INTO creative_editions VALUES(:task_id,:task_revision,:edition,:body,:author)",
                artifact,
            )
            runner.write(
                tmp_path / f"editor-task-{phase}.json",
                {"task": "task", "revision": rev, "citations": citations},
            )
            runner.write(
                browser / f"{phase}.json",
                {
                    "artifact": artifact,
                    "steps": [],
                    "id": "task",
                    "revision": rev,
                    "status": "completed",
                    "message": body,
                    "memory_citations": citations,
                },
            )
            editions[phase] = {
                "edition": 1,
                "items": [
                    first,
                    {"title": title, "media": f"{phase}.mp4", "start": 10 * rev, "end": 10 * rev + 3},
                ],
            }
        db.execute("INSERT INTO agent_tasks VALUES('task',?,'[]')", (body,))
    runner.write(browser / "browser.json", {"schema": 1})
    runner.write(browser / "p2-memory.json", memory)
    monkeypatch.setattr(evidence, "verify", lambda *a, **kw: {"task": "task", "editions": dict(editions)})
    monkeypatch.setattr(evidence, "items", lambda *a: editions["r3"]["items"])
    monkeypatch.setattr(evidence, "inspect_file", lambda *a: None)
    result = evidence.verify_memory(tmp_path, browser)
    assert result["withdrawal_changed_title_ending"] and result["independent_semantic_review"] == "required"
    with closing(sqlite3.connect(tmp_path / "studio.sqlite3")) as db, db:
        db.execute(
            "DELETE FROM memory_source_tombstones WHERE source_key=?", (memory["original"]["source_key"],)
        )
    with pytest.raises(Mismatch, match="已撤回版本的来源"):
        evidence.verify_memory(tmp_path, browser)


@pytest.mark.skipif(not os.getenv("TUBER_TEST_CHROMIUM"), reason="requires local Chromium")
async def test_chat_memory_migration_in_chromium(settings, monkeypatch):
    import uvicorn
    from playwright.async_api import async_playwright, expect

    from tuber_studio.agent import private_call, voice_origin
    from tuber_studio.app import create_app

    app = create_app(settings)
    runtime = app.state.runtime
    base = runtime.llm
    reviews = []
    examples = {
        runner.AUTO: ("以后写直播素材提纲", "每个标题都用问句，以问号结尾", "标题"),
        runner.CONFIRMED: ("when they improve readability", "Use headings and lists", "headings"),
    }

    class Model:
        def __getattr__(self, name):
            return getattr(base, name)

        async def generate_assistant_json(self, messages, schema, **options):
            return {"action": "chat", "reply_markdown": "好的，我会按这个格式写标题。"}

        async def generate_json(self, messages, schema, **options):
            title = schema["title"]
            if title == "OwnerUtterance":
                text = messages[-1]["content"]
                if text in examples:
                    scope, practice, _ = examples[text]
                    return {"meaning": "持续合作方式", "statement": text, "scope_quote": scope, "practice_quote": practice}
                return {"meaning": "社交交谈", "statement": text, "scope_quote": "", "practice_quote": ""}
            assert title == "AgreementReview"
            text = json.loads(messages[-1]["content"])["original"]
            answer = {"meaning": "号主本人的持续协作约定", "scope_supported": True, "practice_supported": True,
                      "same_agreement": True, "topic_quote": examples[text][2], "existing_id": None,
                      "summary_kind": "preference"}
            assert set(answer) == set(schema["required"])
            reviews.append(text)
            return answer

        generate_operator_json = generate_json

    monkeypatch.setattr(runtime, "llm", Model())
    old_quote = "上次提纲里的开场"
    cited = {"id": 91, "revision": 1, "note": old_quote, "quote": old_quote, "source_key": "turn:" + str(uuid4())}
    task = {"id": str(uuid4()), "revision": 1, "status": "completed", "message": "提纲已整理好。",
            "memory_citations": [cited], "steps": [{"revision": 1, "result": {"memory_citations": [cited],
                "memory_proposals": [{"source_id": "fixture:0", "quote": old_quote}]}}]}
    character = runtime.character
    created_at = time.time() - 2
    for message, extra in (("聊聊上次的提纲", {"memory_citations": [cited]}),
                           ("整理上次的提纲", {"card": {"type": "agent_task", "data": task}})):
        saved = {"account_id": "", "private": True, "reply_text": "看过上次的开场了。", **extra}
        runtime.store.execute("INSERT INTO operator_chat_turns VALUES(?,?,?,?,?,?)",
                              (str(uuid4()), character["id"], character["version"], message, json.dumps(saved), created_at))
        created_at += 1

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", lifespan="off",
                                               timeout_graceful_shutdown=2))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(.05)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(executable_path=os.getenv("CHROMIUM_EXECUTABLE"))
                try:
                    context = await browser.new_context(service_workers="block")
                    await context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(origin + "/")
                                        else route.abort())
                    page = await context.new_page()
                    writes = []
                    page.on("request", lambda r: writes.append(r.url) if r.method == "POST" else None)
                    await page.goto(origin)
                    turns = page.locator(".operator-chat-turn")
                    await expect(turns).to_have_count(2)
                    for turn in await turns.all():
                        await runner.check_chat_controls(turn)
                    await expect(page.get_by_role("button", name="查看任务")).to_be_visible()
                    await expect(page.get_by_text("提纲已整理好。", exact=True)).to_be_visible()
                    runtime.memory_organizer.start()
                    scenario = runner.MemoryScenario(origin, settings.runtime, SimpleNamespace(turn_timeout=60))
                    layout = await scenario.preference(page, runner.CONFIRMED, "layout")
                    row = await scenario.preference(page, runner.AUTO, "original")
                    assert reviews == [runner.CONFIRMED, runner.AUTO]
                    assert not any(url.endswith(("/memory.propose", "/memory/confirm")) for url in writes)
                    await page.reload()
                    await expect(turns).to_have_count(4)
                    for turn in await turns.all():
                        await runner.check_chat_controls(turn)
                    await expect(page.get_by_text("提纲已整理好。", exact=True)).to_be_visible()
                    await expect(turns.locator(".operator-chat-user")).to_have_text([
                        "聊聊上次的提纲", "整理上次的提纲", runner.CONFIRMED, runner.AUTO])
                    async with runner.settings_entries(page) as entries:
                        _, evidence = await runner.open_source(page, entries, row)
                        assert evidence["quote"] == runner.AUTO and evidence["source_key"] == row["source_key"]
                    await page.locator('a[data-page="settings"]').click()
                    await page.locator('a[data-workspace-tab="personalization"]').click()
                    history = page.locator("#memory-history-enabled")
                    async with page.expect_response(lambda r: r.url.endswith("/daily-memory/preferences") and r.request.method == "PUT"):
                        await history.uncheck()
                    await page.reload()
                    await expect(history).to_be_enabled()
                    await expect(history).not_to_be_checked()
                    enabled = page.locator("#memory-enabled")
                    for on in (False, True):
                        async with page.expect_response(lambda r: r.url.endswith("/daily-memory/preferences")
                                                        and r.request.method == "PUT") as saved:
                            await enabled.set_checked(on)
                        assert (await (await saved.value).json())["enabled"] is on
                        await page.reload()
                        await expect(enabled).to_be_enabled()
                        assert await enabled.is_checked() is on
                        await expect(history).not_to_be_checked()
                    await page.locator('a[data-page="chat"]').click()
                    scenario.report["corrected"] = row
                    await scenario.forget_title(page)
                    await scenario.current(page, layout)
                    # Seed an already confirmed voice source, then inspect it through settings.
                    # This fixture does not exercise transcription or a live call.
                    key = "browser-fixture"
                    monkeypatch.setattr(private_call, "private_service", lambda settings: (origin, key))
                    signed = voice_origin.ticket_context(key, "fixture-ticket", runtime.tasks.scope())
                    quote = "以后标题用陈述句。"
                    machine = "好的，" + quote
                    signed.update(capture_id="voice-fixture", samples=16000, machine_text=machine,
                                  receipt=voice_origin.sign(key, ["omni.mic.v1", signed["ticket_hash"],
                                                                  "voice-fixture", 16000, machine]))
                    body = {"source": "confirmed_voice", "text": quote, "voice_origin": signed}
                    preview = await scenario.api(page, "POST", "/api/agent/memory.propose", body)
                    confirmed = await scenario.api(page, "POST", "/api/agent/memory/confirm",
                                                   body | {"stamp": preview["stamp"]})
                    voice = await scenario.api(page, "POST", "/api/agent/memory.get", {"id": confirmed["id"]})
                    scenario.report.update(citation_surface="settings_current", corrected=voice, r2_api=[layout, voice],
                                           voice={"bound_final_transcript": machine, "owner_confirmed_text": quote})
                    await scenario.inspect_settings(page, [layout, voice], "r2")
                    assert scenario.report["r2_voice_source_visible"] == f"机器转写：{machine}\n号主确认：{quote}"
                    check_citations(scenario.report, "r2", [layout, voice], [layout, voice])
                finally:
                    await browser.close()
        finally:
            await runtime.memory_organizer.close()
            server.should_exit = True
            await serving
            await app.state.comments.adapter.close()
            runtime.store.close()
