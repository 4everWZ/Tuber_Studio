"""Execute explicitly granted private data transformations on the Studio host."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import Field

from ..core.models import StrictModel


class CodeFile(StrictModel):
    name: str = Field(max_length=100, pattern=r"^[\w -]+\.(txt|md|csv|json|srt|vtt)$")
    script: str = Field(min_length=1, max_length=12000)


def has_code_grant(steps):
    return any(s["tool"] == "owner_code_grant" and s["revision"] == 0 for s in steps)


def private_tools(tools, context):
    if has_code_grant(context):
        return {name: tool for name, tool in tools.items() if name in {"delegate_text", "private_code"}}
    if any(s["tool"] in {"owner_attachment", "authorized_inbox_text"} for s in context):
        return {name: tool for name, tool in tools.items() if name == "delegate_text"}
    return {name: tool for name, tool in tools.items() if name != "private_code" and not tool.fixed_only}


async def run_code(request, args):
    if not has_code_grant(request.receipts):
        return {"error": "该任务没有号主授予的代码执行权限。"}
    inputs = [s["result"] for s in request.receipts if s["tool"] == "owner_attachment" and s["revision"] == 0]
    payload = json.dumps({"files": inputs, "script": args.script}, ensure_ascii=True).encode()
    worker = Path(__file__).with_name("private_code_worker.py")
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    with tempfile.TemporaryDirectory() as folder:
        proc = subprocess.Popen(
            [sys.executable, "-I", str(worker)], cwd=folder, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.to_thread(proc.communicate, payload, timeout=6)
        except subprocess.TimeoutExpired:
            return {"error": "隔离计算超时。请缩小输入或简化计算。"}
        finally:
            if proc.poll() is None:
                proc.kill()
                await asyncio.to_thread(proc.wait)
    if proc.returncode:
        return {"error": "隔离计算失败，未得到文件。"}
    result = json.loads(stdout)
    return result if "error" in result else {"name": args.name, "text": result["text"]}
