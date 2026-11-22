"""Independent read-only comparison of the browser record, workstation database and clips."""

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing

from evaluations.agent.creative_browser_receipt import (
    DEFAULT_BV,
    inspect_file,
    items,
    load,
    overlap,
    require,
    verify,
)
from evaluations.memory.owner_memory_device import AUTO, CONFIRMED
from evaluations.memory.owner_memory_voice import VOICE_RULE

REPLACE = "保留第一段，请再给这份提纲换一个适合开场的第二段，先别发。"
WRITE_TABLES = ("account_writes", "publish_attempts", "dm_attempts", "agent_reply_attempts")


def counts(db, tables):
    return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def check_citations(memory, phase, wire, editor):
    visible = memory[phase + "_visible"]

    def records(rows):
        return Counter((r["id"], r["revision"], r["source_key"], r["quote"], r.get("note")) for r in rows)

    require(
        records(wire) == records(memory[phase + "_api"]) == records(editor),
        f"{phase} 编辑、任务接口与浏览器引用快照不符",
    )
    if memory.get("citation_surface") == "settings_current":
        expected = [{k: row[k] for k in ("id", "source_key", "quote", "note")} for row in wire]
        require(Counter(tuple(sorted(row.items())) for row in visible) ==
                Counter(tuple(sorted(row.items())) for row in expected), f"{phase} 设置中的当前内容或来源不符")
    elif memory.get("citation_surface") == "settings":
        expected_settings = [
            {"quote": r["quote"], "history_path": f"/api/v1/persona/daily-memory/organized/{r['id']}/revisions",
             "source_key": r["source_key"], "revision": r["revision"],
             "visible_revision": f"v{r['revision']} · {r['note']}"}
            for r in wire
        ]
        require(
            Counter(tuple(sorted(r.items())) for r in visible)
            == Counter(tuple(sorted(r.items())) for r in expected_settings),
            f"{phase} 设置原话、来源或可见版本不符",
        )
    else:
        # Existing private receipts predate the settings-only inspection surface.
        require("citation_surface" not in memory, "未知的浏览器引用查验入口")
        require(
            Counter((r["quote"], r["source"]) for r in visible)
            == Counter((r["quote"], f"来源 {r['source_key']} · v{r['revision']}") for r in wire),
            f"{phase} 缺页面原话或来源版本",
        )
    expected = [memory["layout"]]
    if phase != "r3":
        expected.append(memory["original" if phase == "r1" else "corrected"])
    require(
        records(wire) == records(expected),
        f"{phase} 的偏好引用与当时有效的条目及原话不符",
    )


def title_changes(editions):
    first, revised, withdrawn = [editions["r" + str(rev)]["items"] for rev in (1, 2, 3)]
    require(all(i["title"].endswith(("？", "?")) for i in first), "r1 标题未落实问号约定")
    require(
        revised[0] == first[0] and revised[1]["title"].endswith(("。", ".")),
        "r2 首项或新第二项的句号不符合要求",
    )
    require(
        withdrawn[0] == revised[0] and not any(overlap(withdrawn[1], old) for old in revised),
        "撤回后未保留第一项并交付新的第二项",
    )
    return {phase: [i["title"] for i in result["items"]] for phase, result in editions.items()}


def check_voice(memory, database):
    row = memory["corrected"]
    voice = memory["voice"]
    require(
        voice.get("status") == "observed"
        and voice.get("protocol") == "explicit_user_audio"
        and voice.get("signed_final_received") is True
        and voice.get("confirmation_dialog_accepted") is True
        and voice.get("confirm_http_status") == 200,
        "缺已签最终转写或号主明确确认",
    )
    original = memory["original"]
    require(
        voice["correction"] == {"id": original["id"], "revision": original["revision"] + 1}
        and (row["id"], row["revision"]) == (original["id"], original["revision"] + 1)
        and row["source_key"].startswith("confirmed_voice:"),
        "语音更正来源或版本不符",
    )
    confirmed, machine = voice["owner_confirmed_text"], voice["bound_final_transcript"]
    require(
        re.sub(r"[^\w]", "", confirmed) == re.sub(r"[^\w]", "", VOICE_RULE) and row["quote"] == confirmed,
        "号主确认稿与授权约定不符",
    )
    require(
        database
        == {
            "id": row["id"],
            "revision": row["revision"],
            "source_key": row["source_key"],
            "machine_text": machine,
            "confirmed_text": confirmed,
            "samples": voice["explicit_samples"],
            "capture_id": voice["capture_id"],
            "source_bound": True,
            "signature_verified": True,
        }
        and row.get("voice_origin")
        == {
            "capture_id": voice["capture_id"],
            "samples": voice["explicit_samples"],
            "machine_text": machine,
            "owner_edited": machine != confirmed,
        },
        "工作站签名、转写原稿、来源或采样回执不符",
    )


def verify_memory(runtime, receipts):
    host = load(runtime / "p2-host.json")
    memory = load(receipts / "p2-memory.json")
    require(
        host["run"] == memory["run"] == runtime.name and host["denied"] == 0, "工作站实例或出站拦截记录不符"
    )
    baseline = (
        "operator_chat_turns",
        "agent_tasks",
        "creative_editions",
        "daily_highlights",
        "memory_source_tombstones",
        *WRITE_TABLES,
    )
    require(host["baseline"] == dict.fromkeys(baseline, 0), "缺空库基线")
    require(host["platform"] == dict.fromkeys(WRITE_TABLES, 0), "隔离实例记录了平台尝试")
    require(
        all(
            host["inference"].get(path, 0) > 0
            for path in ("/v1/chat/completions", "/llm/v1/responses", "/v1/browser-tickets")
        ),
        "缺普通工作站推理或私有通话签票的成功传输",
    )
    require(load(receipts / "browser.json").get("schema") == 1, "缺同场双浏览器详细回执")
    workflow = verify(runtime, DEFAULT_BV, 2, receipts=receipts, final_revision=3)
    task = workflow["task"]
    editions = workflow["editions"]
    uri = (runtime / "studio.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        require(not any(counts(db, WRITE_TABLES).values()), "数据库有平台写入尝试")
        require(db.execute("SELECT count(*) FROM agent_tasks").fetchone()[0] == 1, "有额外的任务")
        for label, quote in (("layout", CONFIRMED), ("original", AUTO)):
            row = memory[label]
            turn = memory[label + "_turn"]
            saved = db.execute(
                "SELECT message,result FROM operator_chat_turns WHERE request_id=?", (turn,)
            ).fetchone()
            require(
                row["source_key"] == "turn:" + turn
                and row["quote"] == quote
                and saved
                and saved["message"] == quote
                and json.loads(saved["result"]).get("memory_intent") == "chat",
                "偏好没有绑定普通聊天原话",
            )
        layout, original, corrected = (memory[k] for k in ("layout", "original", "corrected"))
        check_voice(memory, load(runtime / "p2-voice-source.json"))
        require(
            memory.get("r2_voice_source_visible")
            == "机器转写："
            + memory["voice"]["bound_final_transcript"]
            + "\n号主确认："
            + memory["voice"]["owner_confirmed_text"],
            "缺 r2 语音来源展开回执",
        )
        require(
            original["id"] == corrected["id"] != layout["id"]
            and corrected["revision"] == original["revision"] + 1,
            "同一偏好的更正链不符",
        )
        require(memory["withdrawn"] == {"id": corrected["id"], "deleted": True}, "撤回条目错位")
        require(
            not db.execute(
                "SELECT 1 FROM confirmed_voice_origins WHERE item_id=?", (corrected["id"],)
            ).fetchone(),
            "撤回后运行库仍保留转写原稿",
        )
        survivor = db.execute(
            "SELECT revision,quote,owner_principal,character_id FROM daily_highlights WHERE id=?",
            (layout["id"],),
        ).fetchone()
        require(
            survivor and (survivor["revision"], survivor["quote"]) == (layout["revision"], CONFIRMED),
            "撤回影响了独立排版偏好",
        )
        require(
            not db.execute("SELECT 1 FROM daily_highlights WHERE id=?", (corrected["id"],)).fetchone(),
            "被撤回条目仍在运行库中",
        )
        for row in (original, corrected):
            require(
                db.execute(
                    "SELECT 1 FROM memory_source_tombstones WHERE owner=? AND character=? AND source_key=?",
                    (survivor["owner_principal"], survivor["character_id"], row["source_key"]),
                ).fetchone(),
                "工作站缺已撤回版本的来源记录",
            )
        turn = db.execute(
            "SELECT message,result FROM operator_chat_turns WHERE request_id=?", (memory["withdrawal_turn"],)
        ).fetchone()
        require(turn and turn["message"] == REPLACE, "撤回后的聊天修订原文错位")
        result = json.loads(turn["result"])
        require(
            result.get("memory_intent") == "steer_task" and result["card"]["data"]["id"] == task,
            "撤回后的交办偏离原任务",
        )
        first_blocks = []
        for rev in (1, 2, 3):
            phase = f"r{rev}"
            snapshot = load(receipts / f"{phase}.json")
            editor = load(runtime / f"editor-{task}-{phase}.json")
            require((editor["task"], editor["revision"]) == (task, rev), "工作站编辑快照修订错位")
            check_citations(memory, phase, snapshot.get("memory_citations", []), editor["citations"])
            first_blocks.append(
                next(block for block in snapshot["artifact"]["body"].split("\n\n") if block.startswith("**"))
            )
        require(first_blocks[0] == first_blocks[1] == first_blocks[2], "保留首项的完整正文发生变化")
        third = snapshot["artifact"]
        saved = db.execute(
            "SELECT * FROM creative_editions WHERE task_id=? AND task_revision=3 AND edition=?",
            (task, third["edition"]),
        ).fetchone()
        current = db.execute("SELECT message,steps FROM agent_tasks WHERE id=?", (task,)).fetchone()
        require(
            saved
            and dict(saved) == third
            and saved["author"] == "agent"
            and (snapshot["id"], snapshot["revision"], snapshot["status"]) == (task, 3, "completed")
            and third["body"] == current["message"] == snapshot["message"],
            "r3 与工作站持久成品不符",
        )
        segments = items(runtime, third["body"], json.loads(current["steps"]), DEFAULT_BV, 2)
        require(
            segments == items(runtime, third["body"], snapshot["steps"], DEFAULT_BV, 2),
            "r3 原件与浏览器证据不符",
        )
        inspect_file(runtime / segments[1]["media"], segments[1])
        editions["r3"] = {"edition": third["edition"], "items": segments}
    titles = title_changes(editions)
    return {
        "workflow": "passed",
        "memory_sources": "observed",
        "titles": titles,
        "withdrawal_changed_title_ending": not titles["r3"][1].endswith(("。", ".")),
        "task": task,
        "platform_write_attempts": 0,
        "editions": editions,
        "independent_semantic_review": "required",
    }
