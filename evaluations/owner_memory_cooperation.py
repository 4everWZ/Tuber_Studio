"""Check ordinary private chat admission before creating any title preferences in the QA runtime."""

from uuid import uuid4

from evaluations.owner_memory_device import SEARCH, device_call
from evaluations.owner_memory_video import await_admission, forget_current
from tuber_studio.cooperation import bound_statement


async def verify_chat_admission(client, runtime, device, port, token, deadline, record):
    cases = [("辛苦啦，跟我聊两句吧。", "skipped"),
             ("咱俩对稿有个默契：存疑处圈红，定稿处描蓝。", "saved")]
    receipts = []
    for quote, expected in cases:
        request_id = str(uuid4())
        turn = {"request_id": request_id, "message": quote}
        source = "turn:" + request_id
        response = await client.post("/api/chat/intelligent", json=turn)
        response.raise_for_status()
        receipts.append({"source": source, "owner_quote": quote, "expected": expected})
        record("cooperation_admission", receipts)
    await await_admission("organize saved QA cooperation examples", runtime.memory_organizer.run_once,
                          deadline, [], record)
    for case in receipts:
        source = case["source"]
        marker = runtime.store.one("SELECT status FROM memory_organization WHERE source=?", (source,))
        remote = await device_call(device, port, token, "POST", SEARCH, {"query": source})
        rows = [row for row in remote["value"]["items"] if row["source_key"] == source]
        case.update(status=marker and marker["status"], remote_source_matches=len(rows))
        record("cooperation_admission", receipts)
        assert case["status"] == case["expected"]
        if case["expected"] == "skipped":
            assert rows == []
        else:
            row, = rows
            scope = runtime.memory_organizer.scope()
            saved, = runtime.daily_memory.organized(scope, item_id=row["id"])
            rule = saved["proof"].get("cooperation_rule")
            quote = case["owner_quote"]
            assert row["quote"] == quote and bound_statement(quote, rule)
            case.update(rule=rule, scope_quote=quote[slice(*rule["scope_span"])],
                        practice_quote=quote[slice(*rule["practice_span"])],
                        proposed_scope_quote=quote[slice(*rule["proposed_scope_span"])])
            record("cooperation_admission", receipts)
            assert case["scope_quote"] == "咱俩对稿有个默契"
            # Keep the following three editions independent of this additional QA preference.
            await forget_current(client, port, device, token, [row])
        record("cooperation_admission", receipts)
