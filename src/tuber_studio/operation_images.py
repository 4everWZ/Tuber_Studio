"""Durable scoped illustration decisions and PNGs for operations drafts."""
import asyncio
import hashlib
import json
import time
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import Field

from .artwork_references import resolve_reference
from .http_api import InferenceBudgetExceeded
from .models import StrictModel
from .responses_provider import ResponsesClient
from .store import dump
from .workstation_images import ImageBrief, MODEL, REVISION, gateway, gateway_base, image_payload, result_png, submit


class ImageScope(StrictModel):
    account_id: str = Field(min_length=1, max_length=200)
    character_id: str = Field(min_length=1, max_length=100)
    channel: Literal["comment", "direct_message", "dynamic"]
    source_id: str = Field(min_length=1, max_length=512)


def scope_key(scope):
    return ImageScope.model_validate(scope).model_dump_json()


async def choose(settings, material):
    # Only the background service receives this channel's material.
    base = gateway_base(settings).removesuffix("/v1")
    if settings.data.get("responses", {}).get("base_url", "").rstrip("/") != base + "/llm/v1":
        raise ValueError("工作站后台 Responses 未配置")
    tool = {"type": "function", "name": "choose_illustration", "strict": True,
            "description": "给当前草稿选纯文字、Q 版或常规版角色配图。",
            "parameters": {"type": "object", "additionalProperties": False,
                           "properties": {"choice": {"type": "string", "enum": ["text_only", "q", "mature"]},
                                          "prompt": {"type": "string", "description": "图中可见场景；纯文字则留空。"}},
                           "required": ["choice", "prompt"]}}
    async with ResponsesClient.from_settings(settings) as client:
        turn = await client.create([{"role": "user", "content": material}], tools=[tool], tool_choice="required",
                                   instructions="你为当前渠道的这条草稿决定配图。先看 context 和 draft："
                                   "图片能展示这次交流中具体的事物、动作或情绪时，可选择生成。"
                                   "正文已完整回答且图片没有新增信息时选择 text_only，prompt 留空。"
                                   "时间确认通常只需文字。角色卡用于保持形象，不能凭角色设定另起一个场景。"
                                   "需要画图时选择 q 或 mature，prompt 描述本次材料中的可见场景。")
    if len(turn.calls) != 1:
        raise ValueError("后台未给出唯一配图决定")
    action, = turn.calls
    if action.name != "choose_illustration" or set(action.arguments) != {"choice", "prompt"}:
        raise ValueError("后台配图决定无效")
    if action.arguments["choice"] == "text_only":
        return None
    return ImageBrief(variant=action.arguments["choice"], prompt=action.arguments["prompt"])


class OperationImages:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.wake = asyncio.Event()
        self.worker = None
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS operation_image_jobs(
                job_id TEXT PRIMARY KEY, scope TEXT NOT NULL, request_key TEXT NOT NULL,
                material TEXT NOT NULL, status TEXT NOT NULL, brief TEXT, endpoint TEXT,
                remote_id TEXT, artifact_id TEXT, error TEXT, created REAL NOT NULL,
                UNIQUE(scope,request_key));
            CREATE INDEX IF NOT EXISTS operation_image_pending ON operation_image_jobs(status,created);
            CREATE TABLE IF NOT EXISTS operation_image_artifacts(
                artifact_id TEXT PRIMARY KEY, scope TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL, content BLOB NOT NULL, model TEXT NOT NULL,
                revision TEXT NOT NULL, variant TEXT NOT NULL, reference_sha256 TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_image_references(
                job_id TEXT NOT NULL REFERENCES operation_image_jobs(job_id), variant TEXT NOT NULL,
                character_version INTEGER NOT NULL, source TEXT NOT NULL, revision TEXT NOT NULL,
                sha256 TEXT NOT NULL, PRIMARY KEY(job_id,variant));
            CREATE TABLE IF NOT EXISTS operation_image_gateway_receipts(
                job_id TEXT PRIMARY KEY REFERENCES operation_image_jobs(job_id),
                state TEXT NOT NULL, seed INTEGER, timing TEXT);
        """)

    def decide(self, scope, *, request_key, context, draft, character_version=None):
        scope = ImageScope.model_validate(scope)
        if not isinstance(request_key, str) or not 0 < len(request_key) <= 512 or not isinstance(draft, str):
            raise ValueError("业务来源键或草稿无效")
        if not isinstance(context, (str, dict, list)):
            raise ValueError("配图上下文须为渠道材料")
        bound = scope_key(scope)
        row = self.store.one("SELECT job_id,material FROM operation_image_jobs WHERE scope=? AND request_key=?",
                             (bound, request_key))
        if row:
            old = json.loads(row["material"])
            if (old["context"] != context or old["draft"] != draft
                    or character_version is not None and old["character"].get("version") != character_version):
                raise ValueError("已绑定的请求键不能替换渠道材料或草稿")
            return self.status(scope, row["job_id"])
        card = self.store.character(scope.character_id, character_version)
        material = dump({"character": {k: card.get(k) for k in ("name", "core", "style", "version")},
                         "channel": scope.channel, "context": context, "draft": draft})
        if len(material.encode()) > 128 * 1024:
            raise ValueError("渠道材料过大")
        identity = uuid4().hex
        bindings = []
        for variant in ("q", "mature"):
            try:
                ref = resolve_reference(self.settings, scope.character_id, variant)
            except (FileNotFoundError, LookupError, ValueError):
                continue
            bindings.append((identity, variant, card["version"], ref.source,
                             str(getattr(ref, "revision", ref.sha256)), ref.sha256))
        with self.store.db as db:
            db.execute("INSERT INTO operation_image_jobs(job_id,scope,request_key,material,status,created) "
                       "VALUES(?,?,?,?,?,?)", (identity, bound, request_key, material, "queued", time.time()))
            db.executemany("INSERT INTO operation_image_references VALUES(?,?,?,?,?,?)", bindings)
        self.wake.set()
        return self.status(scope, identity)

    def status(self, scope, job_id):
        row = self.store.one("SELECT j.job_id,j.status,j.artifact_id,j.error,g.state AS remote_state,g.seed,"
                             "r.character_version,r.variant,r.source AS reference_source,r.sha256 AS reference_sha256,"
                             "r.revision AS reference_revision "
                             "FROM operation_image_jobs j LEFT JOIN operation_image_gateway_receipts g "
                             "ON g.job_id=j.job_id LEFT JOIN operation_image_references r "
                             "ON r.job_id=j.job_id AND r.variant=json_extract(j.brief,'$.variant') "
                             "WHERE j.scope=? AND j.job_id=?",
                             (scope_key(scope), job_id))
        if row is None:
            raise LookupError("配图任务不存在")
        return row

    def read(self, scope, artifact_id):
        row = self.store.one("SELECT a.*,r.source AS reference_source,r.revision AS reference_revision,"
                             "r.character_version FROM operation_image_artifacts a JOIN operation_image_references r "
                             "ON r.job_id=a.job_id AND r.variant=a.variant WHERE a.scope=? AND a.artifact_id=?",
                             (scope_key(scope), artifact_id))
        if row is None:
            raise LookupError("配图工件不存在")
        row.pop("scope")
        return row | {"mime_type": "image/png", "width": 1024, "height": 1024}

    def start(self):
        if self.worker and not self.worker.done():
            return
        self.store.execute("UPDATE operation_image_jobs SET status='queued' WHERE status='deciding'")
        self.store.execute("UPDATE operation_image_jobs SET status='failed',error='远端提交结果未确认，未重投' "
                           "WHERE status='submitting'")
        self.worker = asyncio.create_task(self._run())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None

    async def _run(self):
        while True:
            self.wake.clear()
            job = self.store.one("SELECT * FROM operation_image_jobs WHERE status IN ('queued','running') "
                                 "ORDER BY created LIMIT 1")
            if not job:
                await self.wake.wait()
                continue
            try:
                await self._process(job)
            except Exception as error:
                message = type(error).__name__
                if isinstance(error, httpx.HTTPStatusError):
                    message = f"工作站返回 HTTP {error.response.status_code}"
                elif isinstance(error, InferenceBudgetExceeded):
                    message = "配图决定超过后台 Responses 预算"
                elif isinstance(error, httpx.TransportError):
                    stage = self.store.one("SELECT status FROM operation_image_jobs WHERE job_id=?", (job["job_id"],))
                    message = ("图片请求的提交结果未确认，未重投" if stage["status"] == "submitting"
                               else "读取后台响应失败")
                elif isinstance(error, ValueError) and type(error) is ValueError:
                    message = str(error)[:240]
                elif isinstance(error, TimeoutError):
                    message = "工作站配图等待超时"
                self.store.execute("UPDATE operation_image_jobs SET status='failed',error=? WHERE job_id=?",
                                   (message, job["job_id"]))

    async def _process(self, job):
        identity = job["job_id"]
        scope = ImageScope.model_validate_json(job["scope"])
        if job["status"] == "queued":
            self.store.execute("UPDATE operation_image_jobs SET status='deciding' WHERE job_id=?", (identity,))
            brief = await choose(self.settings, job["material"])
            if brief is None:
                self.store.execute("UPDATE operation_image_jobs SET status='text_only' WHERE job_id=?", (identity,))
                return
            job["brief"] = brief.model_dump_json()
            job["endpoint"] = gateway_base(self.settings)
            self.store.execute("UPDATE operation_image_jobs SET brief=?,endpoint=? WHERE job_id=?",
                               (job["brief"], job["endpoint"], identity))
        else:
            brief = ImageBrief.model_validate_json(job["brief"])
        if gateway_base(self.settings) != job["endpoint"]:
            raise ValueError("任务绑定的图片网关已变更")
        binding = self.store.one("SELECT sha256 FROM operation_image_references WHERE job_id=? AND variant=?",
                                 (identity, brief.variant))
        if not binding:
            raise ValueError("入队时此角色版本没有可用的所选参考")
        async with gateway(self.settings) as client:
            if not job["remote_id"]:
                ref = resolve_reference(self.settings, scope.character_id, brief.variant, sha256=binding["sha256"])
                if hashlib.sha256(ref.data).hexdigest() != binding["sha256"]:
                    raise ValueError("图片参考与入队时绑定的内容不一致")
                payload = image_payload(brief, ref.data)
                self.store.execute("UPDATE operation_image_jobs SET status='submitting' WHERE job_id=?", (identity,))
                job["remote_id"] = await submit(client, payload)
                self.store.execute("UPDATE operation_image_jobs SET status='running',remote_id=? WHERE job_id=?",
                                   (job["remote_id"], identity))
            remote_id = job["remote_id"]
            previous_state = None
            async with asyncio.timeout(3600):
                while True:
                    try:
                        response = await client.get(f"images/jobs/{remote_id}")
                    except httpx.TransportError:
                        await asyncio.sleep(3)
                        continue
                    response.raise_for_status()
                    remote = response.json()
                    if remote["state"] != previous_state:
                        self.store.execute("INSERT OR REPLACE INTO operation_image_gateway_receipts VALUES(?,?,?,?)",
                                           (identity, remote["state"], remote.get("seed"), dump(remote.get("timing"))))
                        previous_state = remote["state"]
                    if remote["state"] == "completed":
                        break
                    if remote["state"] not in ("queued", "running", "cancelling"):
                        raise ValueError("工作站图片任务终止：" + str(remote.get("error") or remote["state"])[:120])
                    await asyncio.sleep(2)
                png = await result_png(client, remote_id)
        artifact_id = uuid4().hex
        with self.store.db as db:
            db.execute("INSERT INTO operation_image_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                       (artifact_id, job["scope"], identity, hashlib.sha256(png).hexdigest(), png, MODEL, REVISION,
                        brief.variant, binding["sha256"]))
            db.execute("UPDATE operation_image_jobs SET status='ready',artifact_id=? WHERE job_id=?",
                       (artifact_id, identity))
