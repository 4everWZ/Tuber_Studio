"""Authorized one-submit Bilibili publication for verified Remotion outputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from pydantic import Field

from ...core.models import StrictModel
from ...core.store import dump
from ...infra.platforms.bilibili.video_upload import (
    VideoSubmissionRejected,
    upload_bilibili_video_web,
    verify_bilibili_video_publication,
    video_receipt_ids,
)
from ...operations import facade as operations
from .remotion_assets import _parse_biliup_receipt, _scrub_secrets


class StageOrPublishVideoArgs(StrictModel):
    job_id: str = Field(min_length=1, max_length=80, description="已通过成片质检的视频任务 ID。")
    publish_mode: Literal["preview_review", "auto_publish"] | None = None
    approved: bool = False
    expected_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class StageVideoToolArgs(StrictModel):
    job_id: str = Field(min_length=1, max_length=80, description="已通过成片质检的视频任务 ID。")


_ELIGIBLE = frozenset({"rendered", "ready_for_review", "publish_failed"})


def _file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class VideoPublicationMixin:
    def _publication_host(self):
        return self._browsing_service() or getattr(self.runtime, "operations", None)

    def _auto_publish_gate_check(self, *, scheduled=False):
        policy = self.policy()
        if policy.publish_mode != "auto_publish" or not policy.auto_publish_enabled:
            return "当前策略未开启自动投稿，保持人工预览审核队列"
        if (browsing := self._browsing_service()) is not None:
            snap = operations.heartbeat_pulse_snapshot(browsing, store=self.store)
            blocked_states = {"emergency", "blocked", "stopping"} if scheduled else {
                "off", "paused", "emergency", "blocked", "stopping",
            }
            if snap.get("control_state") in blocked_states:
                return f"运营控制面状态为 {snap['control_state']}，暂不自动投稿"
            if (snap.get("discovery") or {}).get("read_cooldown_until"):
                return "当前账号处于平台风控冷却期，已转回人工预览审核"
        return None

    def _publication_result(self, job, action, **extra):
        status = job["status"]
        return {"ok": status in {"published", "ready_for_review"}, "job_id": job["id"], "action": action,
                "status": status, "intent": job.get("intent"), "receipt": job.get("receipt"),
                "write_token": job.get("write_token"), "platform_state": job.get("platform_state"),
                "public_url": job.get("public_url"), "error": job.get("error")} | extra

    def _refresh_video_ledger(self, job):
        if job["status"] not in {"publishing", "publish_uncertain"} or not job.get("write_token"):
            return job
        attempt = operations.find_write_attempt_by_intent(
            self._publication_host(), account_id=job["account_id"],
            character_id=job["character_id"], intent=job["intent"],
        ) or {}
        if attempt.get("token") != job["write_token"]:
            return job
        status = attempt.get("resolved_status")
        receipt = attempt.get("receipt")
        if status == "published" and video_receipt_ids(receipt):
            self._update_job(job["id"], status="published", receipt_json=dump(receipt), platform_state="submitted", error=None)
        elif status in {"published", "uncertain"} and job["status"] == "publishing":
            self._update_job(job["id"], status="publish_uncertain", platform_state="unconfirmed",
                             error="重启前的投稿尚未取得有效平台回执")
        elif status in {"rejected", "cancelled"}:
            self._update_job(job["id"], status="publish_failed", platform_state=None, error=attempt.get("error"))
        return self.get_job(job["id"])

    async def refresh_video_publication(self, job_id: str):
        """Recover the stored receipt and refresh its public visibility without submitting."""
        job = self.get_job(job_id)
        if job is None:
            raise ValueError("找不到指定视频任务")
        job = self._refresh_video_ledger(job)
        if job["status"] != "published" or not video_receipt_ids(job.get("receipt")):
            return self._publication_result(job, "readback", reason=(
                "提交结果待核对，尚无有效稿件编号" if job["status"] in {"publishing", "publish_uncertain"} else None
            ))
        if not job.get("account_id"):
            return self._publication_result(job, "readback", reason="旧回执缺少归属账号")
        found = await verify_bilibili_video_publication(job["receipt"], job["account_id"])
        self._update_job(job_id, platform_state=found["state"], platform_checked_at=time.time(),
                         public_url=found.get("url"))
        return self._publication_result(self.get_job(job_id), "readback", reason=found.get("reason"))

    async def _upload_via_biliup(self, *, plan, output_path, cover_path, credentials, timeout, submission_guard, digest):
        if not callable(self.biliup_runner):
            return await upload_bilibili_video_web(
                title=plan.title, desc=plan.summary[:250], tid=plan.category_tid, tags=list(plan.tags[:10]),
                output_path=output_path, cover_path=cover_path, credentials=credentials, timeout=timeout,
                submission_guard=submission_guard, expected_output_digest=digest,
            )
        values = {name: credentials.get(key) for name, key in (("SESSDATA", "sessdata"), ("bili_jct", "csrf"),
                                                                ("DedeUserID", "uid"))}
        cookies = {"cookie_info": {"cookies": [{"name": n, "value": str(v)} for n, v in values.items() if v]}}
        with tempfile.TemporaryDirectory(prefix="biliup-cookie-") as tmp_dir:
            cookie_file = Path(tmp_dir) / "cookies.json"
            cookie_file.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
            with suppress(OSError):
                os.chmod(cookie_file, 0o600)
            cover_args = ["--cover", str(cover_path)] if cover_path else []
            cmd = ["biliup", "--user-cookie", str(cookie_file), "upload", "--submit", "web", "--title", plan.title,
                   "--desc", plan.summary[:250], "--tid", str(plan.category_tid), "--tag", ",".join(plan.tags[:10]),
                   *cover_args, str(output_path)]
            async with submission_guard():
                code, stdout, stderr = await self.biliup_runner(cmd, output_path.parent, timeout)
        if code != 0:
            raise TimeoutError("biliup 未确认提交结果，等待核对原投稿")
        receipt = video_receipt_ids(_parse_biliup_receipt(_scrub_secrets(stdout, credentials), _scrub_secrets(stderr, credentials)))
        return {"platform": "bilibili", "uploader": "biliup_cli", **receipt}

    async def stage_or_publish_bilibili_video(self, arguments, *, scheduled_guard: Callable[[], None] | None = None):
        """Stage a render or submit it through the verified shared write ledger."""
        args = arguments if isinstance(arguments, StageOrPublishVideoArgs) else StageOrPublishVideoArgs.model_validate(arguments)
        job = self.get_job(args.job_id)
        if job is None or not job.get("plan"):
            raise ValueError("找不到指定视频任务")
        if args.expected_content_hash and job.get("content_hash") != args.expected_content_hash:
            raise ValueError("当前成片与已确认版本不同，请重新查看成片")
        job = self._refresh_video_ledger(job)
        if job["status"] == "published" and video_receipt_ids(job.get("receipt")):
            return self._publication_result(job, "already_published")
        if job["status"] in {"publishing", "publish_uncertain"} or job.get("receipt"):
            return self._publication_result(job, "blocked_uncertain")
        if job["id"] in self.active_job_ids or job["id"] in self.active_publication_ids:
            raise ValueError("该视频任务正在渲染或投稿")
        character = self._current_character()
        plan = self._verified_publication_plan(job, character)
        qc = job.get("qc_report") or {}
        if not qc.get("passed") or job["status"] not in _ELIGIBLE:
            raise ValueError("成片尚未通过 FFmpeg/FFprobe 质检，不能投稿")
        policy = self.policy()
        mode = args.publish_mode or policy.publish_mode
        if mode == "preview_review" and not args.approved and scheduled_guard is None:
            preview_intent = operations.canonical_intent("bilibili", "video_publish", job["content_hash"], character["id"])
            self._update_job(job["id"], status="ready_for_review", publish_mode="preview_review", intent=preview_intent)
            return self._publication_result(self.get_job(job["id"]), "staged_for_review", title=plan.title,
                                            output_path=job["output_path"])
        if qc.get("dry_run"):
            raise ValueError("干跑只预检工程，真实渲染后才能投稿")
        automatic = not args.approved or scheduled_guard is not None
        if automatic and (reason := self._auto_publish_gate_check(scheduled=scheduled_guard is not None)):
            self._update_job(job["id"], status="ready_for_review", error=reason)
            return self._publication_result(self.get_job(job["id"]), "gated_to_review", ok=False, reason=reason)
        self.active_publication_ids.add(job["id"])
        try:
            return await self._publish_video_job(job, plan, character, automatic=automatic,
                                                 scheduled_guard=scheduled_guard, timeout=policy.upload_timeout_seconds)
        finally:
            self.active_publication_ids.discard(job["id"])

    async def _publish_video_job(self, job, plan, character, *, automatic, scheduled_guard, timeout):
        account = getattr(self.runtime, "account", None)
        if account is None or not callable(getattr(account, "account_id", None)):
            raise ValueError("平台账号核验服务未就绪")
        async with account.credential_lock:
            credentials = dict(account.credentials)
            if not credentials.get("sessdata") or not credentials.get("csrf"):
                raise ValueError("缺少有效的 B 站登录 Cookie 凭证，无法执行投稿")
            account_id = str(await account.account_id())
            if credentials.get("uid") and str(credentials["uid"]) != account_id:
                raise ValueError("B 站凭据内的 UID 与实际登录账号不一致")
            if (job["account_id"] and job["account_id"] != account_id) or (scheduled_guard and not job["account_id"]):
                raise ValueError("当前登录账号与视频任务绑定账号不一致")
            if str(job["character_id"]) != character["id"] or automatic and job["character_version"] != character["version"]:
                raise ValueError("当前角色与视频任务绑定版本不一致")

        if scheduled_guard is not None:
            scheduled_guard()
        try:
            output_file, _ = self.resolve_job_media(job["id"], "video")
            cover_file = self.resolve_job_media(job["id"], "cover")[0] if job.get("cover_path") else None
        except FileNotFoundError as error:
            raise ValueError("已审核成片或封面已不在工作区，请重新渲染") from error
        digest = await asyncio.to_thread(_file_digest, output_file)
        if digest != job.get("content_hash"):
            raise ValueError("成片与质检时的文件不同，请重新渲染并检查成片")

        host = self._publication_host()
        intent = operations.canonical_intent("bilibili", "video_publish", digest, character["id"])
        binding = {"job_id": job["id"], "title": plan.title, "content_hash": digest}
        existing = operations.find_write_attempt_by_intent(host, account_id=account_id, character_id=character["id"], intent=intent)
        retry_token = None
        if existing:
            status, token, receipt = existing.get("resolved_status"), existing.get("token"), existing.get("receipt")
            if status == "published" and video_receipt_ids(receipt):
                self._update_job(job["id"], account_id=account_id, status="published", intent=intent, write_token=token,
                                 receipt_json=dump(receipt), platform_state="submitted", error=None)
                return self._publication_result(self.get_job(job["id"]), "already_published")
            if status in {"published", "sending", "uncertain"}:
                target_status = "publishing" if status == "sending" else "publish_uncertain"
                self._update_job(job["id"], status=target_status, account_id=account_id, intent=intent, write_token=token,
                                 platform_state="unconfirmed", error="同一成片已有在途或待核对投稿")
                return self._publication_result(self.get_job(job["id"]), "blocked_uncertain")
            if status == "rejected" and not automatic:
                retry_token = token
            else:
                return self._publication_result(job, "previous_attempt_failed", ok=False, error=existing.get("error"))

        token = None
        control = operations.control_policy(host) or {}
        epoch = control.get("epoch") if automatic and scheduled_guard is None else None

        @asynccontextmanager
        async def submission_guard():
            nonlocal token
            async with account.credential_lock:
                if account.credentials != credentials or account.display_account_id() != account_id:
                    raise ValueError("上传期间账号凭据已变化，请重新确认")
                if automatic and (reason := self._auto_publish_gate_check(scheduled=scheduled_guard is not None)):
                    raise ValueError(reason)
                self._verified_publication_plan(job, character)

                def admit(write_token):
                    result = self.store.db.execute(
                        "UPDATE remotion_video_jobs SET status='publishing',account_id=?,intent=?,write_token=?,publish_mode=?,"
                        "platform_state=NULL,error=NULL,updated_at=? WHERE id=? AND status=? AND content_hash=? AND updated_at=?",
                        (account_id, intent, write_token, "auto_publish" if automatic else "preview_review",
                         time.time(), job["id"], job["status"], digest, job["updated_at"]),
                    )
                    if result.rowcount != 1:
                        raise ValueError("成片或审核状态已变化，请重新核对")

                token = operations.reserve_write_attempt(
                    host, account_id=account_id, character=character, intent=intent, channel="video_publish", automatic=automatic,
                    epoch=epoch, binding=binding, content_hash=digest, retry_rejected_token=retry_token,
                    admission_hook=admit, scheduled_guard=scheduled_guard,
                )
                yield

        try:
            async with asyncio.timeout(timeout):
                receipt = await self._upload_via_biliup(
                    plan=plan, output_path=output_file, cover_path=cover_file, credentials=credentials,
                    timeout=timeout, submission_guard=submission_guard, digest=digest,
                )
            if token is None or not video_receipt_ids(receipt):
                raise TimeoutError("B 站尚未返回可确认的投稿回执")
        except asyncio.CancelledError:
            if token:
                operations.finish_write_attempt(host, token, "uncertain", error="投稿期间取消，等待核对")
                self._update_job(job["id"], status="publish_uncertain", platform_state="unconfirmed", error="投稿期间取消，等待核对")
            raise
        except Exception as error:
            message = _scrub_secrets(str(error) or "投稿请求未完成", credentials)
            if token is None:
                status, action, state = "publish_failed", "prepare_failed", None
            elif isinstance(error, VideoSubmissionRejected):
                operations.finish_write_attempt(host, token, "rejected", error=message)
                status, action, state = "publish_failed", "rejected", None
            else:
                operations.finish_write_attempt(host, token, "uncertain", error=message)
                status, action, state = "publish_uncertain", "uncertain", "unconfirmed"
            self.store.execute("UPDATE remotion_video_jobs SET status=?,platform_state=?,error=?,updated_at=? "
                               "WHERE id=? AND ((? IS NULL AND updated_at=?) OR write_token=?)",
                               (status, state, message, time.time(), job["id"], token, job["updated_at"], token))
            return self._publication_result(self.get_job(job["id"]), action, ok=False, error=message)
        operations.finish_write_attempt(host, token, "published", receipt=receipt)
        self._update_job(job["id"], status="published", receipt_json=dump(receipt), platform_state="submitted", error=None)
        return self._publication_result(self.get_job(job["id"]), "published")
