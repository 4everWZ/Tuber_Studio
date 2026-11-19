import asyncio
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    monkeypatch.setattr(runner, "private_service", lambda: ("https://voice.invalid", "fixture-key"))

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
                    await http.post("https://voice.invalid/v1/browser-tickets", json={"path": "/v1/realtime"})
                ).status_code == 503
                with pytest.raises(Mismatch, match="拒绝对外写入"):
                    await http.post("https://voice.invalid/v1/browser-tickets", json={"path": "/v1/other"})
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
