"""Read-only checks shared by the live Chromium evaluation and archived receipt replay."""

import json
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

from tuber_studio.agent.public_video import clip_path

DEFAULT_BV = "BV1Hy4y1J7xH"
CHANGE = "保留第一段，第二段换成适合开场的"
CHAT = "辛苦啦，跟我聊两句吧。"


class Mismatch(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Mismatch(message)


def video_url(bvid, part):
    return f"https://www.bilibili.com/video/{bvid}/?p={part}"


def initial(bvid, part):
    return f"帮我准备下场直播。看我给的视频 P{part} {video_url(bvid, part)}，只取两个不剧透的片段，再查一下原出处，先别发。"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def items(directory, body, steps, bvid, part):
    """Start from the delivered text. Bind each visible citation to this revision's reads."""
    upload = video_url(bvid, part)
    require("投稿简介：" not in body, "实际成果带入了投稿简介")
    reads = [(s, s["result"]) for s in steps if s.get("result")]
    searched, opened = set(), set()
    for step, result in reads:
        if step["tool"] == "web_search":
            searched.update(result.get("links", []))
        elif (
            step["tool"] == "web_page"
            and result.get("excerpt")
            and step["arguments"].get("web_fetch") in searched
        ):
            opened.add(result["url"])
    pages = {r["url"]: r["excerpt"] for s, r in reads if s["tool"] == "web_page" and r.get("excerpt")}
    require(upload in pages, "缺投稿分 P 的网页正文")
    byline = re.search(r"UP 主[：:]\s*(.+?)[。\n]", pages[upload])
    require(byline, "缺可核对的投稿署名")
    originals = {s["id"]: r.get("clip") or {} for s, r in reads if s["tool"] == "inspect_video"}
    entries = []
    for block in body.split("\n\n"):
        if not block.startswith("**"):
            continue
        lines = block.splitlines()
        require(
            len(lines) >= 3 and lines[0].endswith("**") and lines[1].startswith("选材理由："),
            "成果项缺标题或理由",
        )
        match = re.fullmatch(r"主帧：(BV\w+) · 单帧采样 · 分 P (\d+) · ([\d.]+) 秒 · <([^>]+)>", lines[2])
        require(match and (match[1], int(match[2])) == (bvid, part), "成果主帧与指定 BV 或分 P 不符")
        seconds = float(match[3])
        require(match[4] == f"{upload}&t={int(seconds)}", "主帧跳转时码不符")
        sources = set(re.findall(r"<(https://[^>]+)>", "\n".join(lines[3:])))
        external = sorted(
            url
            for url in sources & opened
            if not (urlsplit(url).hostname or "").endswith(".bilibili.com")
            and urlsplit(url).hostname != "bilibili.com"
        )
        require(
            upload in sources and external and byline[1] in re.sub(r"\\(.)", r"\1", block),
            "该项缺投稿署名或检索后实际读取的外部出处",
        )
        matches = {}
        for step, receipt in reads:
            clip = receipt.get("clip") or {}
            if (
                step["tool"] != "inspect_video"
                or clip.get("bvid") != bvid
                or clip.get("part") != part
                or not clip.get("cid")
                or not (clip["start"] <= seconds < clip["end"])
                or not 0 < clip["end"] - clip["start"] <= 10
                or clip["frames"] < 2
            ):
                continue
            if not any(
                f.get("seconds") == seconds
                and f.get("url") == match[4]
                and f.get("part") == part
                and str(f.get("cid")) == str(clip["cid"])
                and f.get("kind") == "实际视频帧"
                for f in receipt.get("findings", [])
            ):
                continue
            key = receipt.get("request_id", step["id"])
            require(
                key in originals
                and all(
                    originals[key].get(k) == clip[k]
                    for k in ("bvid", "part", "cid", "start", "end", "frames")
                ),
                "短片未绑定本任务原读取回执",
            )
            media = clip_path(directory, key)
            require(media.is_file() and media.stat().st_size > 0, "缺送审短片文件")
            matches[key] = dict(
                cid=str(clip["cid"]),
                start=clip["start"],
                end=clip["end"],
                frames=clip["frames"],
                media=str(media.relative_to(directory).as_posix()),
            )
        require(len(matches) == 1, "成果主帧无法唯一绑定至已保存的短片")
        clip = next(iter(matches.values()))
        require(
            re.search(rf"\bCID\s+{re.escape(clip['cid'])}\b", pages[upload]), "投稿页分 P 的 CID 与短片不符"
        )
        coverage = (
            f"- 第 {len(entries) + 1} 项 · {bvid} P{part} · 连续取材入点 {clip['start']:g} 秒、"
            f"出点 {clip['end']:g} 秒（不含），保存 {clip['frames']} 个连续原帧。"
        )
        require(coverage in body, "成果所列短片区间或帧数与读取回执不符")
        entries.append(
            dict(
                title=lines[0][2:-2],
                rationale=lines[1].removeprefix("选材理由："),
                url=match[4],
                seconds=seconds,
                sources=external,
                **clip,
            )
        )
    require(len(entries) == 2, f"该版实际交付 {len(entries)} 项，双段替换场景未完成")
    a, b = entries
    require(a["cid"] == b["cid"] and not overlap(a, b), "同版两项 CID 不一致或窗口重叠")
    return entries


def overlap(a, b):
    return a["start"] < b["end"] and b["start"] < a["end"]


def same_position(left, right):
    return (
        left
        and right
        and left.get("turn")
        and left["turn"] == right.get("turn")
        and all(abs(left[k] - right[k]) <= 1 for k in ("offset", "scroll"))
    )


def inspect_file(path, clip):
    require(shutil.which("ffprobe"), "需要 ffprobe 核对保存的实际短片")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-f",
            "mov",
            "-count_frames",
            "-show_entries",
            "stream=codec_type,nb_read_frames,duration_ts,time_base",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=30,
    )
    require(probe.returncode == 0, "已关联的本地短片无法解码")
    streams = json.loads(probe.stdout)["streams"]
    require(len(streams) == 1 and streams[0]["codec_type"] == "video", "私有短片流类型不符")
    video = streams[0]
    tick = Fraction(video["time_base"])
    require(
        int(video["nb_read_frames"]) == clip["frames"]
        and abs(float(int(video["duration_ts"]) * tick) - (clip["end"] - clip["start"]))
        <= float(tick) + 1e-6,
        "已保存短片实际帧数或时长与成果回执不符",
    )


def verify(directory: Path, bvid: str, part: int, *, receipts=None, final_revision=2):
    receipts = receipts or directory
    browser = load(receipts / "browser.json")
    require(
        browser.get("scenario") == "unhinted"
        and not browser.get("recovery")
        and not any((root / "original-browser.json").exists() for root in (directory, receipts)),
        "仅接受全新无提示交办",
    )
    require(
        browser.get("initial") == initial(bvid, part) and browser.get("change") == CHANGE, "浏览器交办不符"
    )
    require(
        browser.get("assistantVisibleBeforeClose") is True and browser.get("secondBrowserProcess") is True,
        "缺关页前回复或第二个浏览器进程的回执",
    )
    task = browser["task"]
    editions = {}
    uri = (directory / "studio.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        require(not db.execute("SELECT 1 FROM account_writes LIMIT 1").fetchone(), "有平台写入尝试")
        turns = db.execute(
            "SELECT * FROM operator_chat_turns WHERE message IN (?, ?) ORDER BY created_at",
            (browser["initial"], CHANGE),
        ).fetchall()
        require(
            [r["message"] for r in turns] == [browser["initial"], CHANGE], "缺普通交办或自然改口的消息回执"
        )
        if browser.get("schema") == 1:
            require(browser.get("changeTurn") == turns[1]["request_id"], "设备 B 消息与持久改口回执不符")
            side = browser.get("sideChat") or {}
            saved_chat = db.execute(
                "SELECT message, result FROM operator_chat_turns WHERE request_id=?",
                (side.get("turn"),),
            ).fetchone()
            require(
                saved_chat and saved_chat["message"] == side.get("message") == CHAT
                and json.loads(saved_chat["result"] or "{}").get("memory_intent") == "chat",
                "设备 A 的闲聊没有按普通对话结算",
            )
        for row, intent in zip(turns, ["agent_task", "steer_task"], strict=True):
            result = json.loads(row["result"])
            require(
                result.get("memory_intent") == intent
                and (result.get("card") or {}).get("data", {}).get("id") == task,
                "交办或改口未指向同一任务",
            )
        row = db.execute("SELECT * FROM agent_tasks WHERE id=?", (task,)).fetchone()
        require(row and row["status"] == "completed" and row["revision"] == final_revision, "原任务终态不符")
        steps = json.loads(row["steps"])
        for rev in (1, 2):
            snapshot = load(receipts / f"r{rev}.json")
            artifact = snapshot.get("artifact") or {}
            saved = db.execute(
                "SELECT * FROM creative_editions WHERE task_id=? AND task_revision=? AND edition=?",
                (task, rev, artifact.get("edition")),
            ).fetchone()
            require(
                saved
                and saved["author"] == "agent"
                and all(
                    saved[k] == artifact.get(k)
                    for k in ("task_id", "task_revision", "edition", "author", "body")
                )
                and snapshot["id"] == task
                and snapshot["revision"] == rev
                and snapshot["status"] == "completed"
                and saved["body"] == snapshot["message"],
                f"r{rev} 浏览器成果与持久正文不符",
            )
            if rev == final_revision:
                require(saved["body"] == row["message"], "任务当前正文与 r2 不符")
            durable = items(directory, saved["body"], [s for s in steps if s["revision"] <= rev], bvid, part)
            require(
                durable == items(directory, artifact["body"], snapshot["steps"], bvid, part),
                f"r{rev} 下钻所见和持久读取证据不一致",
            )
            editions[f"r{rev}"] = {
                "edition": saved["edition"],
                "items": durable,
            }
    before, after = (v["items"] for v in editions.values())
    require(before[0] == after[0], "首项标题、理由、来源或原片改变")
    require(
        after[1]["cid"] == before[1]["cid"] and not any(overlap(old, after[1]) for old in before),
        "第二项没有替换到同分 P 的新窗口",
    )
    require(
        browser.get("phase") == "browser passed"
        and browser.get("returnedToAnchor") is True
        and browser.get("video") == before[0]["url"],
        "缺历史版原片跳转或阅读位置回执",
    )
    if browser.get("schema") == 1:
        require(
            browser.get("historyEdition") == [1, editions["r1"]["edition"]], "缺可绑定到 r1 的历史成果响应"
        )
        require(
            browser.get("sideChat", {}).get("reply") and browser.get("statusBeforeChat"),
            "缺关页前已完成的闲聊回复或当时的任务状态",
        )
        require(
            same_position(browser.get("beforeAnchor"), browser.get("afterAnchor"))
            and same_position(browser.get("beforeActivity"), browser.get("afterActivity")),
            "返回位置证据不一致",
        )
    for clip in (before[0], before[1], after[1]):
        inspect_file(directory / clip["media"], clip)
    objections = directory / "independent-field-objections.json"
    return {
        "workflow": "passed",
        "browser_receipt": "anchored" if browser.get("schema") == 1 else "legacy",
        "content_review": "separate",
        "task": task,
        "platform_write_attempts": 0,
        "editions": editions,
        "recorded_content_objections": load(objections) if objections.exists() else None,
    }
