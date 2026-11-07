"""Video candidate retrieval and read-only views for unified autonomous operations."""

import asyncio
import json
import re
import time

from ...bilibili_account import PROFILE_CACHE_KEY, signature
from ...operations_control import canonical_intent
from ...providers import public_error
from ...store import dump
from .evidence import EvidenceUnavailable
from .issues import unread_issues
from .models import BrowsePlan, CandidateBatchPlan

ACTIVE_TASKS = ("discovered", "claimed", "preparing", "evidence_ready")


def source_settings(policy):
    sources = policy["sources"] or ["recommend"]
    source = sources[0]
    keywords = policy["keywords"]
    if policy["autonomous_topics"] and not keywords:
        source = "autonomous"
    elif sources == ["recommend"] and keywords:
        source = "search"
    elif source == "search" and not keywords:
        source = "recommend"
    return {"mode": source, "keywords": keywords, "watch_videos": policy["watch_videos"]}


class BackgroundDiscovery:
    def __init__(self, browsing):
        self.browsing = browsing
        self.error = None
        self.attention = None
        self.history_checked_signature = None

    @property
    def enabled(self):
        return self.browsing.operations.policy()["enabled"] and self.browsing.policy()["discovery_enabled"]

    @property
    def round_id(self):
        return next(
            (
                identity
                for identity in self.browsing.active
                if not self.browsing.repo.round(identity)["config"].get("interaction_only")
                and not self.browsing.repo.round(identity)["preview"]
            ),
            None,
        )

    @property
    def state(self):
        if self.browsing.closed:
            return "stopping"
        if self.browsing.starting:
            return "starting"
        return "running" if self.round_id else "waiting" if self.enabled else "off"

    @property
    def next_at(self):
        if not self.enabled:
            return None
        return self.browsing.store.get(
            f"browsing_next:{self.browsing.account}:{self.browsing.runtime.character['id']}"
        )

    def history_scope(self):
        service = self.browsing
        comments = service.comments
        account = comments.display_account_id()
        if not account:
            credential_signature = signature(comments.credentials)
            cached = service.store.get("browsing_discovery_account", {}) or {}
            profile = service.store.get(PROFILE_CACHE_KEY, {}) or {}
            if cached.get("credential_signature") == credential_signature:
                account = cached["account_id"]
            elif profile.get("credential_signature") == credential_signature:
                account = profile["profile"]["uid"]
        character = service.runtime.character
        return account, character["id"], character["version"]

    def remember_account(self, account):
        self.browsing.store.set(
            "browsing_discovery_account",
            {
                "account_id": account,
                "credential_signature": signature(self.browsing.comments.credentials),
            },
        )

    async def restore_history_scope(self):
        """Verify an account once for older histories saved before account binding was persisted."""
        service = self.browsing
        account, character, version = self.history_scope()
        credential = service.comments.credentials
        key = signature(credential)
        if account or not credential.get("sessdata") or self.history_checked_signature == key:
            return
        if not service.store.one(
            """SELECT 1 FROM browsing_rounds WHERE character_id=? AND character_version=?
               AND (json_extract(config,'$.discovery_publish')=1 OR json_extract(config,'$.autonomous_batch')=1) LIMIT 1""",
            (character, version),
        ):
            return
        self.history_checked_signature = key
        try:
            async with asyncio.timeout(4):
                account = await service.comments.account_id()
            self.remember_account(account)
        except (TimeoutError, ValueError):
            # The operator can verify or update credentials in Accounts.
            pass

    async def configure_source(self, selection):
        if not isinstance(selection, dict) or set(selection) != {"mode", "keywords", "watch_videos"}:
            raise ValueError("来源配置需要模式、关键词和指定视频")
        mode, keywords, videos = (selection[key] for key in ("mode", "keywords", "watch_videos"))
        if mode not in ("recommend", "search", "popular", "autonomous", "specified"):
            raise ValueError("请选择有效的发现来源")
        if (
            not isinstance(keywords, list)
            or len(keywords) > 20
            or any(
                not isinstance(word, str) or not word.strip() or len(word.strip()) > 100 for word in keywords
            )
        ):
            raise ValueError("请输入有效关键词，最多 20 个，每个最多 100 字")
        if (
            not isinstance(videos, list)
            or len(videos) > 20
            or any(
                not isinstance(video, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", video)
                for video in videos
            )
        ):
            raise ValueError("请输入有效 BV 号，最多 20 个")
        if mode == "search" and not keywords:
            raise ValueError("关键词搜索至少需要一个关键词")
        if mode == "specified" and not videos:
            raise ValueError("请填写指定视频的 BV 号")
        await self.browsing.configure(
            {
                "sources": ["recommend" if mode == "autonomous" else mode],
                "keywords": list(dict.fromkeys(word.strip() for word in keywords))
                if mode == "search"
                else [],
                "watch_videos": list(dict.fromkeys(videos))
                if mode == "specified"
                else self.browsing.policy()["watch_videos"],
                "autonomous_topics": mode == "autonomous",
            }
        )
        return self.status()

    def status(self):
        service = self.browsing
        active = service.repo.round(self.round_id) if self.round_id else None
        scope = self.history_scope()
        if active and service.repo.retry_scope(active) != scope:
            active = None
        last = service.repo.decode(
            service.store.one(
                """SELECT * FROM browsing_rounds WHERE account_id=? AND character_id=? AND character_version=?
               AND (json_extract(config,'$.discovery_publish')=1 OR json_extract(config,'$.autonomous_batch')=1) AND status NOT IN ('queued','running','archived')
               AND created_at>=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
                (*scope, service.store.get("browsing_history_visible_since", 0)),
            )
        )
        current = None
        progress = {}
        selected_round = active or last
        if selected_round:
            rows = service.store.all(
                """SELECT status, COUNT(*) AS n FROM (
                       SELECT status, ROW_NUMBER() OVER (PARTITION BY bvid ORDER BY created_at DESC, id DESC) AS rank
                       FROM browsing_tasks WHERE round_id=?
                   ) WHERE rank=1 GROUP BY status""",
                (selected_round["id"],),
            )
            progress = {row["status"]: row["n"] for row in rows}
        if active:
            current = service.store.one(
                """SELECT id,bvid,status,COALESCE(json_extract(payload,'$.metadata.title'),
                          json_extract(payload,'$.title'),bvid) AS title
                   FROM browsing_tasks WHERE round_id=? AND status IN (?,?,?,?)
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (active["id"], *ACTIVE_TASKS),
            )
        phase = self.state
        if phase == "running":
            phase = (
                ("judging" if current["status"] == "evidence_ready" else "reading")
                if current
                else "discovering"
                if not progress
                else "finishing"
            )
        control = service.operations.policy()
        attention = self.attention
        if control["emergency"]:
            attention = {
                "message": "紧急停止已生效",
                "label": "打开刷视频设置",
                "href": "#operations-controls",
            }
        elif not attention and not service.comments.policy()["credentials_configured"]:
            attention = {
                "message": "B 站账号缺少登录或写入凭据",
                "label": "更新 B 站账号",
                "href": "#accounts",
            }
        return {
            "enabled": self.enabled,
            "state": "emergency" if control["emergency"] else phase,
            "overlay_enabled": control["enabled"],
            "error": self.error,
            "attention": [attention] if attention else [],
            "next_at": self.next_at,
            "daily_watch": service.repo.quotas.watch_status(
                service.account or control["account_id"],
                service.runtime.character["id"],
                service.policy().get("daily_watch_limit", 0),
                service.policy().get("business_timezone", "Asia/Taipei"),
            ),
            "active_round": active,
            "last_round": last,
            "progress": progress,
            "sent": self.sent_count(selected_round),
            "current_video": current,
            "summary": {
                "processed": sum(
                    count for status, count in progress.items() if status not in (*ACTIVE_TASKS, "cancelled")
                ),
                "skipped": progress.get("skipped", 0),
            },
            "source": source_settings(service.policy()),
            "round_source": source_settings(selected_round["config"])["mode"] if selected_round else None,
            "account_id": service.account or control["account_id"],
            "configured_account_id": service.comments.display_account_id(),
            "character": {key: service.runtime.character[key] for key in ("id", "version", "name")},
            "model": service.settings.data["llm"].get("model"),
        }

    def sent_count(self, round_row):
        if not round_row:
            return 0
        return self.browsing.store.one(
            "SELECT COUNT(*) AS n FROM browsing_intents WHERE round_id=? AND status='published'",
            (round_row["id"],),
        )["n"]

    def records(self, *, recent=False):
        store = self.browsing.store
        scope = self.history_scope()
        if recent:
            return {"activity": self.activity(scope), "scope": scope}
        sent = store.all(
            """SELECT i.id,i.kind,i.updated_at,i.text,json_extract(i.receipt,'$.rpid') AS receipt_id,
                      json_extract(i.target,'$.bvid') AS bvid
               FROM browsing_intents i JOIN browsing_rounds r ON r.id=i.round_id
               WHERE (json_extract(r.config,'$.discovery_publish')=1 OR json_extract(r.config,'$.autonomous_batch')=1) AND i.status='published'
               AND r.account_id=? AND r.character_id=? AND r.character_version=?
               ORDER BY i.updated_at DESC LIMIT 30""",
            scope,
        )
        issues = store.all(
            """SELECT t.id,t.round_id,t.bvid,t.error,t.updated_at,t.status
               FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id
               WHERE (json_extract(r.config,'$.discovery_publish')=1 OR json_extract(r.config,'$.autonomous_batch')=1) AND t.error IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM browsing_batches b WHERE b.round_id=r.id)
               AND (t.status='retry_wait' OR json_extract(t.payload,'$.consecutive_failures')=3)
               AND r.account_id=? AND r.character_id=? AND r.character_version=?
               UNION ALL
               SELECT id,id,NULL,COALESCE(error,json_extract(metrics,'$.source_error')),updated_at,status
               FROM browsing_rounds WHERE (json_extract(config,'$.discovery_publish')=1 OR json_extract(config,'$.autonomous_batch')=1)
               AND account_id=? AND character_id=? AND character_version=?
               AND COALESCE(error,json_extract(metrics,'$.source_error')) IS NOT NULL
               ORDER BY updated_at DESC LIMIT 50""",
            (*scope, *scope),
        )
        failures = self.failures(scope)
        issues = self.merge_issues([*failures, *issues])
        if self.attention:
            issues.insert(
                0,
                {
                    "error": self.attention["message"],
                    "href": self.attention["href"],
                    "updated_at": self.attention["at"],
                    "status": "blocked",
                },
            )
        since = store.get("browsing_history_visible_since", 0)
        sent = [item for item in sent if item["updated_at"] >= since]
        issues = unread_issues(self.browsing, [item for item in issues if item.get("updated_at", 0) >= since])
        return {"sent": sent, "issues": issues, "activity": self.activity(scope)}

    @staticmethod
    def merge_issues(items):
        merged = {}
        for item in items:
            key = (item.get("bvid"), item.get("error"))
            if key not in merged:
                merged[key] = dict(item, occurrences=item.get("occurrences", 1))
            else:
                merged[key]["occurrences"] += item.get("occurrences", 1)
        return sorted(merged.values(), key=lambda item: item.get("updated_at", 0), reverse=True)[:50]

    def failures(self, scope):
        rows = self.browsing.store.all(
            """SELECT f.*,b.round_id FROM browsing_batch_failures f
               JOIN browsing_batches b ON b.id=f.batch_id JOIN browsing_rounds r ON r.id=b.round_id
               WHERE r.account_id=? AND r.character_id=? AND r.character_version=?
                 AND (json_extract(r.config,'$.discovery_publish')=1 OR json_extract(r.config,'$.autonomous_batch')=1)
                 AND f.updated_at>=?
               ORDER BY f.updated_at DESC""",
            (*scope, self.browsing.store.get("browsing_history_visible_since", 0)),
        )
        current_status = {}
        if rows:
            for task in self.browsing.store.all(
                """SELECT t.bvid,t.status FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id
                   WHERE r.account_id=? AND r.character_id=? AND r.character_version=?
                   ORDER BY t.updated_at DESC,t.id DESC""",
                scope,
            ):
                current_status.setdefault(task["bvid"], task["status"])
        grouped = {}
        for row in rows:
            key = (row["bvid"], row["signature"])
            if key not in grouped:
                recovered = current_status.get(row["bvid"]) in {
                    "selected",
                    "viewed_without_comment",
                    "comments_checked",
                }
                grouped[key] = {
                    "id": f"failure-{row['bvid']}-{row['signature'][:16]}",
                    "round_id": row["round_id"],
                    "bvid": row["bvid"],
                    "kind": "failure",
                    "status": "recovered" if recovered else "retry_wait",
                    "error": row["error"],
                    "reason": row["error"],
                    "updated_at": row["updated_at"],
                    "occurrences": 0,
                }
            grouped[key]["occurrences"] += row["count"]
        return list(grouped.values())

    def activity(self, scope):
        activity = self.browsing.store.all(
            """WITH rounds AS (
                   SELECT id FROM browsing_rounds WHERE account_id=? AND character_id=? AND character_version=?
                   AND (json_extract(config,'$.discovery_publish')=1 OR json_extract(config,'$.autonomous_batch')=1)
               ), tasks AS (
                   SELECT t.id,t.round_id,t.bvid,t.status,t.updated_at,t.created_at,
                          COALESCE(json_extract(t.payload,'$.metadata.title'),json_extract(t.payload,'$.title')) AS title,
                          COALESCE(NULLIF(json_extract(t.payload,'$.reason'),''),
                                   NULLIF(json_extract(t.payload,'$.explanation'),''),
                                   NULLIF(json_extract(t.payload,'$.decisions[0].explanation'),''),
                                   NULLIF(json_extract(t.payload,'$.decisions[0].reason'),''),t.error) AS reason,
                          json_extract(t.payload,'$.retry_kind') AS retry_kind,
                          json_extract(t.payload,'$.consecutive_failures') AS failures
                   FROM browsing_tasks t JOIN rounds r ON r.id=t.round_id
               ), activity AS (
               SELECT t.id,t.round_id,t.bvid,t.title,t.status,t.updated_at,
                      'task' AS kind,NULL AS excerpt,t.reason,t.failures,NULL AS recipient_name,t.retry_kind
               FROM tasks t WHERE t.status IN ('viewed_without_comment','skipped','retry_wait','cancelled')
               AND NOT EXISTS (SELECT 1 FROM browsing_batches b WHERE b.round_id=t.round_id)
               UNION ALL
               SELECT i.id,i.round_id,json_extract(i.target,'$.bvid'),
                      (SELECT t.title FROM tasks t WHERE t.round_id=i.round_id
                       AND t.bvid=json_extract(i.target,'$.bvid') AND t.title IS NOT NULL
                       ORDER BY t.created_at DESC, t.id DESC LIMIT 1),
                      i.status,i.updated_at,i.kind,substr(i.text,1,160),i.reason,NULL,
                      json_extract(i.target,'$.recipient_name'),NULL
               FROM browsing_intents i JOIN rounds r ON r.id=i.round_id
               WHERE i.kind IN ('top_level','reply_public') AND i.status IN ('published','publish_uncertain','automatic_skipped')
               ), ranked AS (
                   SELECT *,ROW_NUMBER() OVER (PARTITION BY (kind='task') ORDER BY updated_at DESC,id DESC) AS position
                   FROM activity
               )
               SELECT id,round_id,bvid,title,status,updated_at,kind,excerpt,reason,failures,recipient_name,retry_kind
               FROM ranked WHERE position<=5 ORDER BY updated_at DESC,id DESC""",
            scope,
        )
        failures = self.failures(scope)
        merged = {}
        replies = []
        for item in sorted([*activity, *failures[:5]], key=lambda item: item["updated_at"], reverse=True):
            if item["kind"] not in {"task", "failure"}:
                replies.append(item)
                continue
            key = (item["bvid"], item.get("error") or item.get("reason"))
            if key not in merged:
                merged[key] = dict(item, occurrences=item.get("occurrences", 1))
            else:
                merged[key]["occurrences"] += item.get("occurrences", 1)
        since = self.browsing.store.get("browsing_history_visible_since", 0)
        return sorted(
            [item for item in [*replies, *merged.values()] if item["updated_at"] >= since],
            key=lambda item: item["updated_at"],
            reverse=True,
        )


async def discover(browsing, row):
    config = row["config"]
    batch = browsing.repo.batches.ensure(row)
    if batch and batch["plan"] is not None:
        config["batch_workers"] = batch["plan"]["workers"]
        return browsing.repo.batches.candidates(row, pending=True)
    retries = browsing.repo.retry_candidates(row) if config.get("autonomous_batch") else []
    if len(retries) >= config["inspect_limit"]:
        return await browsing._plan_batch(row, retries)
    try:
        return await browsing._discover_candidates(row, retries)
    except Exception as error:
        if not retries:
            raise
        metrics = browsing.repo.round(row["id"])["metrics"] | {"source_error": public_error(error)}
        browsing.store.execute("UPDATE browsing_rounds SET metrics=? WHERE id=?", (dump(metrics), row["id"]))
        return retries


async def discover_candidates(browsing, row, retries):
    config = row["config"]
    batch = browsing.repo.batches.get(row)
    retrieval = batch["retrieval"] if batch else {}
    if retrieval.get("complete"):
        return await browsing._select_batch_candidates(row, retries, browsing.repo.batches.candidates(row))
    sources = list(config["sources"]) or ["recommend"]
    keywords = list(dict.fromkeys(config["keywords"]))[: config["max_search_terms"]]
    plan = None
    if retrieval.get("source_plan"):
        sources, keywords = retrieval["source_plan"]["sources"], retrieval["source_plan"]["keywords"]
    elif config["autonomous_topics"] and not keywords:
        feedback = []
        for task in browsing.store.all(
            "SELECT t.payload,t.material_key FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE r.character_id=? AND t.status IN ('selected','viewed_without_comment') ORDER BY t.updated_at DESC LIMIT 6",
            (row["character_id"],),
        ):
            try:
                raw = browsing.evidence.load(task["material_key"])
                feedback.append(
                    {
                        "title": raw["metadata"]["title"],
                        "decisions": json.loads(task["payload"]).get("decisions", []),
                    }
                )
            except EvidenceUnavailable:
                continue
        plan = await browsing.decisions._model(
            row,
            BrowsePlan,
            {"public_interests": browsing.runtime.character["core"], "public_browsing_feedback": feedback},
            "只提出有界浏览计划。空词默认recommend；search必须明确返回真实关键词；可以wait。不得使用私人记忆。",
        )
        browsing.store.execute(
            "UPDATE browsing_rounds SET metrics=? WHERE id=?",
            (dump({"plan": plan.model_dump()}), row["id"]),
        )
        if plan.action == "wait":
            browsing.store.set(
                "browsing_next:" + row["account_id"] + ":" + row["character_id"],
                time.time() + plan.delay_seconds,
            )
            if batch:
                browsing.repo.batches.save_plan(row, {"videos": [], "workers": 1}, presented=[])
            return retries
        sources, keywords = [plan.action], plan.keywords
        if plan.action == "search" and (not keywords or any(not k.strip() or len(k) > 100 for k in keywords)):
            raise ValueError("OC搜索计划无有效关键词")
    elif keywords and sources == ["recommend"]:
        sources = ["search"]
    if batch:
        retrieval["source_plan"] = {"sources": sources, "keywords": keywords}
        browsing.repo.batches.retrieval(row, retrieval)
    merged = {item["bvid"]: item for item in browsing.repo.batches.candidates(row)} if batch else {}
    if batch:
        for item in browsing.repo.batches.deferred_candidates(row):
            merged.setdefault(item["bvid"], item)
    for source in sources:
        if len(merged) >= config["candidate_limit"]:
            break
        if source == "specified":
            for video in config["watch_videos"]:
                merged.setdefault(video, {"bvid": video, "sources": []})["sources"].append(
                    {"source": "specified"}
                )
            continue
        if source == "search" and not keywords:
            source = "recommend"
        for keyword in keywords if source == "search" else [""]:
            cursor_key = f"browsing_source_page:{row['account_id']}:{row['character_id']}:{source}:{keyword}"
            query_key = dump([source, keyword])
            start_page = 1
            if batch and source in {"search", "popular"}:
                start_page = retrieval.setdefault("starts", {}).setdefault(
                    query_key, max(1, int(browsing.store.get(cursor_key, 1)))
                )
                browsing.repo.batches.retrieval(row, retrieval)
            for page in range(start_page, start_page + config["max_pages"]):
                page_key = dump([source, keyword, page])
                saved_page = retrieval.get("pages", {}).get(page_key)
                if saved_page is not None:
                    if not saved_page["has_more"] or len(merged) >= config["candidate_limit"]:
                        break
                    continue
                result = await browsing._read(
                    browsing.adapter.discover, source, keyword, page, config["candidate_limit"]
                )
                for item in result["items"]:
                    record = merged.setdefault(item["bvid"], dict(item, sources=[]))
                    record["sources"].append(
                        {
                            "source": source,
                            "keyword": keyword,
                            "page": page,
                            "plan_reason": plan.reason if plan else "user_configuration",
                        }
                    )
                if batch:
                    browsing.repo.batches.merge(row, list(merged.values()))
                    retrieval.setdefault("pages", {})[page_key] = {"has_more": result.get("has_more", False)}
                    browsing.repo.batches.retrieval(row, retrieval)
                    if source in {"search", "popular"}:
                        browsing.store.set(cursor_key, page + 1 if result.get("has_more", False) else 1)
                if len(merged) >= config["candidate_limit"] or not result.get("has_more", False):
                    break
    candidates = list(merged.values())[: config["candidate_limit"]]
    if batch:
        browsing.repo.batches.merge(row, candidates)
        retrieval["complete"] = True
        browsing.repo.batches.retrieval(row, retrieval)
    return await browsing._select_batch_candidates(row, retries, candidates)


async def select_batch_candidates(browsing, row, retries, candidates):
    config = row["config"]
    cursor_key = "browsing_rotation:" + row["account_id"] + ":" + row["character_id"]
    batch = browsing.repo.batches.get(row)
    offset = batch["retrieval"].get("rotation") if batch else None
    if offset is None:
        offset = browsing.store.get(cursor_key, 0) % max(1, len(candidates))
        if batch:
            browsing.repo.batches.retrieval(row, batch["retrieval"] | {"rotation": offset})
    candidates = candidates[offset:] + candidates[:offset]
    browsing.store.set(cursor_key, offset + config["inspect_limit"])
    selected = retries[: config["inspect_limit"]]
    selected_ids = {item["bvid"] for item in selected}
    for item in candidates:
        if row["preview"] and len(selected) >= config["inspect_limit"]:
            break
        if item["bvid"] in selected_ids or (
            config.get("autonomous_batch") and browsing.repo.video_retry(row, item["bvid"])
        ):
            continue
        if config.get("batch_id") and browsing.repo.batches.title_skipped(row, item):
            browsing.repo.batches.omit_candidate(row, item, reason="已记录本条标题的略过选择")
            continue
        if config.get("batch_id") and (next_at := browsing.repo.batches.waiting_until(row, item["bvid"])):
            browsing.repo.batches.omit_candidate(
                row, item, reason="等待本条视频的下一处理时间", next_at=next_at
            )
            continue
        reason = browsing.filter_reason(item, config)
        if reason:
            if config.get("batch_id"):
                browsing.repo.batches.omit_candidate(row, item, reason=reason)
            prior_filter = browsing.store.one(
                "SELECT t.id FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id "
                "WHERE r.account_id=? AND r.character_id=? AND t.bvid=? AND t.status='skipped' AND t.error=? LIMIT 1",
                (row["account_id"], row["character_id"], item["bvid"], reason),
            )
            if prior_filter:
                continue
            identity = browsing.repo.task(row["id"], item)
            browsing.store.execute(
                "UPDATE browsing_tasks SET status='skipped',error=? WHERE id=?", (reason, identity)
            )
            continue
        previous = browsing.store.one(
            "SELECT t.id FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE t.bvid=? AND r.character_id=? AND r.character_version=? AND (t.status IN ('selected','viewed_without_comment') OR (t.status='checkpoint' AND r.account_id=? AND json_extract(t.payload,'$.capacity_signature')=?)) AND t.updated_at>?",
            (
                item["bvid"],
                row["character_id"],
                row["character_version"],
                row["account_id"],
                browsing.decisions._capacity_signature(config),
                time.time() - 21600,
            ),
        )
        if previous and not config.get("force_read") and not config.get("reply_enabled"):
            if config.get("batch_id"):
                browsing.repo.batches.omit_candidate(row, item, reason="已完成内容处理，本轮未开启评论回访")
            continue
        selected.append(item)
        selected_ids.add(item["bvid"])
        if row["preview"] and len(selected) >= config["inspect_limit"]:
            break
    return await browsing._plan_batch(row, selected)


async def plan_batch(browsing, row, candidates):
    # Retry records and retrieval pages are merged before this point. The raw
    # batch keeps all of them; only the bounded window is sent to the model.
    candidates = list({item["bvid"]: item for item in candidates}.values())
    config = row["config"]
    if config.get("batch_id"):
        browsing.repo.batches.merge(row, candidates)
    candidates = candidates[: config["candidate_limit"]]
    if row["preview"] or not candidates:
        if row["config"].get("batch_id"):
            browsing.repo.batches.save_plan(row, {"videos": [], "workers": 1}, presented=[])
        return candidates[: row["config"]["inspect_limit"]]
    plan = await browsing.decisions._model(
        row,
        CandidateBatchPlan,
        {
            "candidates": [browsing._candidate_view(row, item) for item in candidates],
            "max_videos": len(candidates),
            "max_workers": config["workers"],
        },
        "这一批视频已按BV号去重。按你的兴趣决定想看哪些和先后顺序，返回对应BV号；"
        "可以少选或不选。workers是你想同时交给视频子Agent处理的数量，不超过max_workers。"
        "already_commented表示已有视频主回复，不再重复发主回复，但可以继续翻评论、回复新留言。"
        "watch_state区分new、started、reading、viewed和complete；只有fully_read=true才表示完整读过可用内容。"
        "逐条给choices与具体理由：watch新看，comments继续评论，skip略过，later暂后；"
        "videos按要执行的watch/comments顺序排列，later留给后续批次。"
        "子Agent会读取真实字幕和画面，再自行决定发言、回复评论或略过；这一批完成后再看下一批。",
        max_tokens=config["decision_max_tokens"],
    )
    config["batch_workers"] = plan.workers
    metrics = browsing.repo.round(row["id"])["metrics"] | {"batch_plan": plan.model_dump()}
    browsing.store.execute(
        "UPDATE browsing_rounds SET config=?,metrics=? WHERE id=?",
        (dump(config), dump(metrics), row["id"]),
    )
    if config.get("batch_id"):
        browsing.repo.batches.save_plan(
            row, plan.model_dump(), presented=[item["bvid"] for item in candidates]
        )
        return browsing.repo.batches.candidates(row, pending=True)
    by_id = {item["bvid"]: item for item in candidates}
    return [by_id[bvid] for bvid in plan.videos]


def candidate_view(browsing, row, item):
    bvid = item["bvid"]
    progress = browsing.repo.content_progress(row, bvid)
    started = bool(
        browsing.store.one(
            "SELECT 1 FROM browsing_watch_starts WHERE account_id=? AND character_id=? AND bvid=? LIMIT 1",
            (row["account_id"], row["character_id"], bvid),
        )
    )
    visit = browsing.store.one(
        "SELECT MAX(t.updated_at) AS last FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id WHERE r.account_id=? AND r.character_id=? AND t.bvid=?",
        (row["account_id"], row["character_id"], bvid),
    )
    return {key: item.get(key) for key in ("bvid", "title", "owner", "duration", "sources")} | {
        "watch_state": progress["read_state"] if progress else "started" if started else "new",
        "fully_read": bool(progress and progress["fully_read"]),
        "main_decision": progress.get("main_decision") if progress else None,
        "comment_progress": {
            "next_page": browsing.store.get(
                f"browsing_comment_page:{row['account_id']}:{row['character_id']}:{bvid}", 1
            )
        },
        "last_visit": visit["last"] if visit else None,
        "already_commented": bool(
            browsing.store.one(
                "SELECT id FROM browsing_intents WHERE account_id=? AND character_id=? AND source_key=?",
                (
                    row["account_id"],
                    row["character_id"],
                    canonical_intent("bilibili", bvid, "top-level", row["character_id"]),
                ),
            )
        ),
    }
