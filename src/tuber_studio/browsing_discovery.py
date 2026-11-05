"""Opt-in, process-owned video discovery and public posting."""

import asyncio
import re
import time
from uuid import uuid4

from .bilibili_account import PROFILE_CACHE_KEY, signature
from .providers import public_error

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
        self.worker = None
        self.identity = None
        self.binding = None
        self.round_id = None
        self.action = None
        self.next_at = None
        self.state = "off"
        self.error = None
        self.attention = None
        self.history_checked_signature = None

    @property
    def enabled(self):
        return self.worker is not None and not self.worker.done()

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
               AND json_extract(config,'$.discovery_publish')=1 LIMIT 1""",
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
               AND json_extract(config,'$.discovery_publish')=1 AND status NOT IN ('queued','running')
               ORDER BY created_at DESC, id DESC LIMIT 1""",
                scope,
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
            phase = self.action or (
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
        elif control["enabled"]:
            attention = {
                "message": "全渠道运营正在运行",
                "label": "管理全渠道运营",
                "href": "#operations-controls",
            }
        elif (
            self.enabled
            and self.binding
            and service.store.one(
                "SELECT 1 FROM operation_channel_blocks WHERE account_id=? AND channel='comments'",
                (self.binding[0],),
            )
        ):
            attention = {
                "message": "评论渠道有历史回执待核对",
                "label": "打开评论与草稿",
                "href": "#comments",
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
            "account_id": self.binding[0] if self.binding else None,
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
               WHERE json_extract(r.config,'$.discovery_publish')=1 AND i.status='published'
               AND r.account_id=? AND r.character_id=? AND r.character_version=?
               ORDER BY i.updated_at DESC LIMIT 30""",
            scope,
        )
        issues = store.all(
            """SELECT t.id,t.round_id,t.bvid,t.error,t.updated_at,t.status
               FROM browsing_tasks t JOIN browsing_rounds r ON r.id=t.round_id
               WHERE json_extract(r.config,'$.discovery_publish')=1 AND t.error IS NOT NULL
               AND (t.status='retry_wait' OR json_extract(t.payload,'$.consecutive_failures')=3)
               AND r.account_id=? AND r.character_id=? AND r.character_version=?
               UNION ALL
               SELECT id,id,NULL,COALESCE(error,json_extract(metrics,'$.source_error')),updated_at,status
               FROM browsing_rounds WHERE json_extract(config,'$.discovery_publish')=1
               AND account_id=? AND character_id=? AND character_version=?
               AND COALESCE(error,json_extract(metrics,'$.source_error')) IS NOT NULL
               ORDER BY updated_at DESC LIMIT 50""",
            (*scope, *scope),
        )
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
        return {"sent": sent, "issues": issues, "activity": self.activity(scope)}

    def activity(self, scope):
        return self.browsing.store.all(
            """WITH rounds AS (
                   SELECT id FROM browsing_rounds WHERE account_id=? AND character_id=? AND character_version=?
                   AND json_extract(config,'$.discovery_publish')=1
               ), tasks AS (
                   SELECT t.id,t.round_id,t.bvid,t.status,t.updated_at,t.created_at,
                          COALESCE(json_extract(t.payload,'$.metadata.title'),json_extract(t.payload,'$.title')) AS title,
                          COALESCE(json_extract(t.payload,'$.reason'),json_extract(t.payload,'$.decisions[0].reason')) AS reason,
                          json_extract(t.payload,'$.consecutive_failures') AS failures
                   FROM browsing_tasks t JOIN rounds r ON r.id=t.round_id
               )
               SELECT t.id,t.round_id,t.bvid,t.title,t.status,t.updated_at,
                      'task' AS kind,NULL AS excerpt,t.reason,t.failures
               FROM tasks t WHERE t.status IN ('viewed_without_comment','skipped','retry_wait','cancelled')
               UNION ALL
               SELECT i.id,i.round_id,json_extract(i.target,'$.bvid'),
                      (SELECT t.title FROM tasks t WHERE t.round_id=i.round_id
                       AND t.bvid=json_extract(i.target,'$.bvid') AND t.title IS NOT NULL
                       ORDER BY t.created_at DESC, t.id DESC LIMIT 1),
                      i.status,i.updated_at,i.kind,substr(i.text,1,160),i.reason,NULL
               FROM browsing_intents i JOIN rounds r ON r.id=i.round_id
               WHERE i.kind IN ('top_level','reply_public') AND i.status IN ('published','publish_uncertain','automatic_skipped')
               ORDER BY updated_at DESC, id DESC LIMIT 5""",
            scope,
        )

    def start(self):
        service = self.browsing
        if service.closed or service.operations.policy()["emergency"]:
            raise ValueError("浏览已停止或紧急停止仍有效")
        if service.operations.policy()["enabled"] or service.overlay_starting:
            raise ValueError("请先关闭全渠道自主运营，再单独开启自动刷视频")
        if not self.enabled:
            self.identity = str(uuid4())
            self.state, self.error, self.binding = "starting", None, None
            self.attention = None
            self.worker = asyncio.create_task(self._loop())
        return self.status()

    async def stop(self):
        worker = self.worker
        if worker and not worker.done():
            if self.state != "stopping":
                self.state = "stopping"
                self.identity = None
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        if self.worker is worker:
            self.worker = self.identity = self.binding = self.next_at = None
            self.state, self.error = "off", None
            self.attention = None
        return self.status()

    def _binding(self):
        service = self.browsing
        return service.account, service.credential_signature(), service.model_signature()

    def verify(self, identity):
        if (
            self.identity is None
            or identity != self.identity
            or self.binding is None
            or self.binding != self._binding()
            or self.browsing.operations.policy()["emergency"]
        ):
            raise ValueError("刷视频授权、账号、角色或模型已变化，请重新开始")

    async def _loop(self):
        service = self.browsing
        run_id = self.identity
        child = None
        issue_target = ("更新 B 站账号", "#accounts")
        try:
            # A preflight must not replace the adapter of an in-flight manual round.
            while service.active:
                await asyncio.wait(tuple(service.active.values()), return_when=asyncio.FIRST_COMPLETED)
            if not service.comments.policy()["credentials_configured"]:
                raise ValueError("自动发布需要 B 站登录与写入凭据")
            await service._current_account()
            binding = self._binding()
            self.remember_account(binding[0])
            issue_target = ("检查推理与模型", "#settings")
            await service.check_browsing_model()
            issue_target = ("检查发现来源", "#discovery-source-form")
            await service.probe_discovery(service.policy())
            if binding != self._binding():
                raise ValueError("启动检查期间账号、角色或模型已变化")
            self.binding = binding
            issue_target = ("返回自动刷视频", "#automation")
            while True:
                self.verify(self.identity)
                deferred = service.store.get(
                    f"browsing_next:{service.account}:{service.runtime.character['id']}", 0
                )
                if deferred > time.time():
                    self.state = "waiting"
                    self.next_at = deferred
                    await asyncio.sleep(max(0, deferred - time.time()))
                    self.next_at = None
                    continue
                if service.active:
                    self.state = "waiting"
                    await asyncio.wait(tuple(service.active.values()), return_when=asyncio.FIRST_COMPLETED)
                    continue
                try:
                    issue_target = ("更新 B 站账号", "#accounts")
                    await service._current_account(expected=self.binding[0])
                    self.verify(self.identity)
                    self.attention = None
                    if service.active:
                        continue
                    issue_target = ("返回自动刷视频", "#automation")
                    result = await service.run(discovery_id=self.identity)
                    self.round_id = result["id"]
                    child = service.active.get(self.round_id)
                    self.state, self.error = "running", None
                    if child:
                        await asyncio.wait((child,))
                    last = service.repo.round(self.round_id)
                    self.round_id = None
                    child = None
                    self.error = last["error"] if last else None
                except Exception as error:
                    self.error = public_error(error)
                    if issue_target[1] == "#accounts":
                        self.attention = {
                            "message": self.error,
                            "label": issue_target[0],
                            "href": issue_target[1],
                            "at": time.time(),
                        }
                    self.verify(self.identity)
                self.state = "blocked" if self.attention else "waiting"
                delay = service.policy()["interval_seconds"]
                self.next_at = time.time() + delay
                await asyncio.sleep(delay)
                self.next_at = None
        except Exception as error:
            self.state, self.error = "blocked", public_error(error)
            self.attention = {
                "message": self.error,
                "label": issue_target[0],
                "href": issue_target[1],
                "at": time.time(),
            }
        finally:
            if child and not child.done():
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)
            self.round_id = self.action = None
            self.binding, self.next_at = None, None
            service.store.execute(
                """UPDATE browsing_intents SET status='automatic_skipped',error='运行已结束'
                   WHERE status='automatic_pending' AND round_id IN
                   (SELECT id FROM browsing_rounds WHERE json_extract(config,'$.discovery_id')=?)""",
                (run_id,),
            )
