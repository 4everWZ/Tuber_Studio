"""L4 operations control, dual-pipeline (Discovery/Inbox), quotas, claims, intents, and recovery routes."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import Body, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import Field

from ...core.models import StrictModel
from ...operations.activity import acknowledge_issues, activity, clear_issues, history
from ...operations.channels.mentions import bind_mentions
from ...operations.facade import illustration_for_row
from .system import ERROR_RESPONSES

DISCOVERY_SETTING_FIELDS = frozenset(
    {
        "sources",
        "keywords",
        "watch_videos",
        "autonomous_topics",
        "discovery_enabled",
        "top_level_enabled",
        "top_level_auto_publish",
        "reply_enabled",
        "reply_auto_publish",
        "comment_like_enabled",
        "comment_like_auto_publish",
        "video_like_enabled",
        "video_like_auto_publish",
        "dynamic_like_enabled",
        "dynamic_like_auto_publish",
        "video_fav_enabled",
        "video_fav_auto_publish",
        "max_replies_per_video",
        "daily_watch_limit",
        "business_timezone",
        "auto_downgrade_on_error",
        "frame_sampling",
        "workers",
        "candidate_limit",
        "max_search_terms",
        "max_pages",
        "max_reads",
        "max_model_calls",
        "max_input_tokens",
        "max_output_tokens",
        "decision_max_tokens",
        "deadline_seconds",
        "interval_seconds",
        "read_retries",
        "max_takeovers",
        "max_per_hour",
        "max_per_day",
        "top_level_limit",
        "recent_sample_limit",
        "exclude_words",
        "allowed_author_uids",
        "excluded_author_uids",
        "allowed_categories",
        "min_duration_seconds",
        "max_duration_seconds",
        "published_after",
        "published_before",
        "languages",
        "report_enabled",
    }
)

INBOX_SETTING_FIELDS = frozenset(
    {
        "inbox_enabled",
        "mentions_enabled",
        "replies_enabled",
        "mention_reply_enabled",
        "notification_interval_seconds",
        "max_per_hour",
        "max_per_day",
        "recipient_cooldown_seconds",
        "direct_quota_reserve",
    }
)


class IssueObservation(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    updated_at: float = Field(ge=0, allow_inf_nan=False)
    occurrences: int = Field(ge=1, strict=True)


class PipelineToggle(StrictModel):
    enabled: bool = Field(strict=True)


class QuotaControlPatch(StrictModel):
    direct_quota_reserve: int | None = Field(default=None, ge=0, strict=True)
    max_per_hour: int | None = Field(default=None, ge=0, strict=True)
    max_per_day: int | None = Field(default=None, ge=0, strict=True)
    recipient_cooldown_seconds: int | None = Field(default=None, ge=0, le=86400, strict=True)


def _quota_status_snapshot(browsing) -> dict:
    control = browsing.operations.policy()
    account_id = browsing.account or control["account_id"]
    limits = browsing.operations.account_limits()
    max_hour = limits.get("max_per_hour", control["max_per_hour"])
    max_day = limits.get("max_per_day", control["max_per_day"])
    reserve = int(control.get("direct_quota_reserve", 2) or 0)
    now = time.time()
    writes = browsing.sends._account_non_reaction_writes(account_id, now) if account_id else []
    in_flight_older_hour = sum(
        w["resolved_status"] == "sending" and w["started_at"] <= now - 3600 for w in writes
    )
    in_flight_older_day = sum(
        w["resolved_status"] == "sending" and w["started_at"] <= now - 86400 for w in writes
    )
    used_hour = sum(w["started_at"] > now - 3600 for w in writes) + in_flight_older_hour
    used_day = sum(w["started_at"] > now - 86400 for w in writes) + in_flight_older_day
    remaining_hour = max(0, max_hour - used_hour) if max_hour else None
    remaining_day = max(0, max_day - used_day) if max_day else None
    deferral = browsing.sends.direct_reserve_block(account_id, now=now, writes=writes) if account_id else None
    return {
        "account_id": account_id,
        "direct_quota_reserve": reserve,
        "max_per_hour": max_hour,
        "max_per_day": max_day,
        "recipient_cooldown_seconds": control["recipient_cooldown_seconds"],
        "used_per_hour": used_hour,
        "used_per_day": used_day,
        "remaining_per_hour": remaining_hour,
        "remaining_per_day": remaining_day,
        "discovery_deferred": deferral is not None,
        "discovery_deferral_reason": str(deferral) if deferral is not None else None,
        "discovery_retry_at": deferral.next_at if deferral is not None else None,
    }


def register_browsing_routes(app, browsing, require_admin, *, messages=None) -> None:
    """Register `/api/v1/operations/*` autonomous control, Discovery, Inbox, quotas, claims, and activity routes."""
    deps = [Depends(require_admin)]
    prefix = "/api/v1/operations"
    message_service = messages if messages is not None else getattr(browsing, "messages", None)

    @app.get(prefix + "/automation", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_status():
        return browsing.status()

    @app.put(prefix + "/automation/settings", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_settings(data: dict = Body(...)):
        if set(data) == {"discovery_source"}:
            return await browsing.discovery.configure_source(data["discovery_source"])
        if "mode" in data:
            return await browsing.discovery.configure_source(data)
        return await browsing.configure(data)

    @app.post(prefix + "/automation/preflight", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_preflight(data: dict = Body(default={})):
        return await browsing.preflight(for_automation=bool(data.get("for_automation", True)))

    @app.post(prefix + "/automation/enable", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_enable(data: dict = Body(...)):
        if set(data) != {"enabled"}:
            raise ValueError("只接受enabled字段")
        return await browsing.set_enabled(data["enabled"])

    @app.post(prefix + "/automation/review", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_review(data: dict = Body(default={})):
        if set(data) - {"resume_session_id"}:
            raise ValueError("未知review字段")
        if "resume_session_id" in data and (
            not isinstance(data["resume_session_id"], str) or not 1 <= len(data["resume_session_id"]) <= 100
        ):
            raise ValueError("需要原review会话ID")
        return await browsing.start_review_session(**data)

    @app.post(prefix + "/automation/round", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_round(data: dict = Body(default={})):
        if set(data) - {"preview", "video"} or data.get("preview", True) is not True:
            raise ValueError("手工入口只允许只读预览")
        return await browsing.run(preview=True, video=data.get("video"))

    @app.post(prefix + "/automation/stop", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_stop(data: dict = Body(default={})):
        if set(data) - {"emergency"} or type(data.get("emergency", False)) is not bool:
            raise ValueError("emergency须为布尔值")
        return await browsing.stop(emergency=data.get("emergency", False))

    @app.post(prefix + "/automation/reset-stop", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_reset_stop():
        if browsing.operations.draining:
            raise ValueError("当前停运尚未收齐任务")
        browsing.operations.configure({"emergency": False})
        return browsing.status()

    @app.get(prefix + "/discovery", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_discovery():
        await browsing.discovery.restore_history_scope()
        return browsing.discovery.status()

    @app.get(prefix + "/discovery/records", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_discovery_records(recent: bool = False):
        return browsing.discovery.records(recent=recent)

    @app.get(prefix + "/discovery/history", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_discovery_history(
        day: str | None = None,
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=50),
    ):
        await browsing.discovery.restore_history_scope()
        return history(
            browsing,
            day=day,
            page=page,
            page_size=page_size,
            visible_since=browsing.store.get("browsing_history_visible_since", 0),
        )

    @app.post(prefix + "/discovery/enable", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_discovery_enable(data: dict = Body(...)):
        if "enabled" not in data or set(data) - {"enabled"} or type(data["enabled"]) is not bool:
            raise ValueError("enabled须为布尔值且不允许额外字段")
        return await browsing.set_discovery_enabled(data["enabled"])

    @app.put(prefix + "/discovery/settings", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def configure_discovery_settings(data: dict = Body(...)):
        if not data:
            raise ValueError("请提供至少一项探索管道设置")
        if set(data) == {"discovery_source"}:
            return await browsing.discovery.configure_source(data["discovery_source"])
        if "mode" in data:
            return await browsing.discovery.configure_source(data)
        if unknown := set(data) - DISCOVERY_SETTING_FIELDS:
            raise ValueError("未知探索管道设置字段: " + ", ".join(sorted(unknown)))
        await browsing.configure(data)
        return browsing.discovery.status()

    @app.get(prefix + "/inbox", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_inbox():
        return browsing.inbox()

    @app.post(prefix + "/inbox/enable", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_inbox_enable(data: dict = Body(...)):
        if "enabled" not in data or set(data) - {"enabled"} or type(data["enabled"]) is not bool:
            raise ValueError("enabled须为布尔值且不允许额外字段")
        return await browsing.set_inbox_enabled(data["enabled"])

    @app.put(prefix + "/inbox/settings", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def configure_inbox_settings(data: dict = Body(...)):
        if not data:
            raise ValueError("请提供至少一项私信与互动设置")
        if unknown := set(data) - INBOX_SETTING_FIELDS - {"messages"}:
            raise ValueError("未知私信与互动设置字段: " + ", ".join(sorted(unknown)))
        patch = dict(data)
        dm_patch = patch.pop("messages", None)
        if patch:
            await browsing.configure(patch)
        if dm_patch is not None:
            if message_service is None:
                raise ValueError("当前未挂载私信通道")
            if not isinstance(dm_patch, dict):
                raise ValueError("messages 设置必须为对象")
            await message_service.configure(dm_patch)
        return browsing.inbox_status() | ({"messages": message_service.policy()} if message_service else {})

    @app.get(prefix + "/control/quotas", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def get_control_quotas():
        return _quota_status_snapshot(browsing)

    @app.put(prefix + "/control/quotas", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def update_control_quotas(body: QuotaControlPatch):
        patch = body.model_dump(exclude_none=True)
        if not patch:
            raise ValueError("请提供至少一项配额控制字段")
        browsing.operations.configure(
            patch,
            account_id=browsing.account,
            character=browsing.runtime.character,
        )
        browsing.changed.set()
        browsing.interactions_changed.set()
        browsing.sends.changed.set()
        return _quota_status_snapshot(browsing)

    @app.get(prefix + "/claims", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_claims():
        return {"items": browsing.operations.claims.list_claims()}

    @app.get(prefix + "/activity", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_activity(
        day: str | None = None,
        kind: str = "all",
        comment_filter: Literal["all", "read", "replied", "pending_review"] = "all",
        source: Literal["all", "discovery", "inbox", "manual"] = "all",
        page: int = Query(1, ge=1),
        page_size: int = Query(10, ge=1, le=50),
        round_id: str | None = Query(None, max_length=128),
    ):
        await browsing.discovery.restore_history_scope()
        return activity(
            browsing,
            day=day,
            kind=kind,
            comment_filter=comment_filter,
            source=source,
            page=page,
            page_size=page_size,
            round_id=round_id,
            visible_since=browsing.store.get("browsing_history_visible_since", 0),
        )

    @app.post(prefix + "/issues/clear", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_issues_clear():
        return clear_issues(browsing)

    @app.post(prefix + "/issues/acknowledge", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_issues_acknowledge(observations: list[IssueObservation] = Body(min_length=1, max_length=100)):
        return acknowledge_issues(browsing, [item.model_dump() for item in observations])

    @app.get(prefix + "/tasks", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_tasks(
        status: str | None = None,
        round_id: str | None = None,
        task_id: str | None = None,
    ):
        return browsing.tasks(status, round_id=round_id, task_id=task_id)

    @app.post(prefix + "/tasks/{identity}/reread", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_task_reread(identity: str):
        return await browsing.reread(identity)

    @app.get(prefix + "/rounds", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_rounds():
        return browsing.rounds()

    @app.get(prefix + "/intents", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_intents():
        return browsing.intents()

    @app.post(prefix + "/intents/{identity}/review", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_intent_review(identity: str, data: dict = Body(...)):
        if set(data) - {"action", "text", "image_ref", "updated_at", "expected_image", "mentions"}:
            raise ValueError("未知审核字段")
        options = {key: data[key] for key in ("image_ref", "updated_at", "expected_image") if key in data}
        if "mentions" in data:
            row = browsing.repo.decode(browsing.store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
            if not row or data.get("updated_at") != row["updated_at"]:
                raise ValueError("候选已变化，请刷新正文和提及")
            target = row["target"]
            mentions, pending = await bind_mentions(
                browsing.adapter,
                data.get("text", row["text"]),
                data["mentions"],
                [{"uid": target.get("recipient"), "name": target.get("recipient_name")}],
            )
            options["mention_selection"] = {"mentions": mentions, "pending_mentions": pending}
        return browsing.review(identity, data.get("action"), data.get("text"), **options)

    @app.post(prefix + "/intents/{identity}/publish", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def automation_intent_publish(identity: str):
        return await browsing.publish(identity)


def register_operations_routes(
    app, runtime, store, account, comments, browsing, dynamics, messages, require_admin
) -> None:
    """Register `/api/v1/operations/*` browsing controls, illustrations, and continuation recovery routes."""
    register_browsing_routes(app, browsing, require_admin, messages=messages)
    deps = [Depends(require_admin)]
    prefix = "/api/v1/operations"
    channels = {"comments": comments, "messages": messages, "dynamics": dynamics, "browsing": browsing}

    @app.get(
        prefix + "/illustrations/{channel}/{identity}/{artifact_id}",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def illustration(
        channel: Literal["comments", "browsing", "dynamics", "messages"], identity: str, artifact_id: str
    ):
        if channel == "comments":
            row = comments._draft_source(identity)
        elif channel == "browsing":
            row = browsing.repo.decode(store.one("SELECT * FROM browsing_intents WHERE id=?", (identity,)))
        elif channel == "messages":
            row = store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        else:
            row = store.one("SELECT * FROM dynamic_posts WHERE id=?", (identity,))
        if not row or row["account_id"] != account.display_account_id():
            raise HTTPException(404)
        image = illustration_for_row(comments.runtime, comments.memes, channel, row, artifact_id=artifact_id)
        if not image:
            raise HTTPException(404)
        return Response(image["content"], media_type=image["mime_type"], headers={"Cache-Control": "private, no-store"})

    @app.get(prefix + "/recovery-policy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def recovery_policy():
        return {"max_recoveries": runtime.operations.policy()["max_recoveries"]}

    @app.put(prefix + "/recovery-policy", dependencies=deps, responses=ERROR_RESPONSES, tags=["operations"])
    async def configure_recovery_policy(data: dict = Body(...)):
        if set(data) != {"max_recoveries"} or type(data["max_recoveries"]) is not int:
            raise ValueError("只接受整数max_recoveries")
        value = runtime.operations.configure(data)
        return {"max_recoveries": value["max_recoveries"]}

    @app.post(
        prefix + "/continuations/{channel}/{local_id}/resume",
        dependencies=deps,
        responses=ERROR_RESPONSES,
        tags=["operations"],
    )
    async def resume_continuation(
        channel: Literal["comments", "messages", "dynamics", "browsing"],
        local_id: str,
        data: dict = Body(...),
    ):
        if (
            set(data) != {"continuation_id"}
            or not isinstance(data["continuation_id"], str)
            or len(data["continuation_id"]) > 100
        ):
            raise ValueError("需要当前来源的continuation_id")
        service = channels[channel]
        handler = getattr(service, "resume_continuation", None)
        if handler is None:
            raise ValueError("该渠道尚无恢复任务入口")
        return await handler(local_id, data["continuation_id"])
