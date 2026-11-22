"""Bilibili direct-message session polling, history refresh, and worker lifecycle."""

import asyncio
import time

from ....infra.inference.providers import public_error
from ....infra.platforms.bilibili.messages import BilibiliMessagesAdapter


class DirectMessagesSyncMixin:
    async def _adapter(self):
        if self.injected:
            return self.adapter
        credentials = self.account.credentials
        signature = (credentials.get("sessdata"), credentials.get("csrf"))
        if signature != self.credential_signature or self.adapter is None:
            if self.adapter:
                await self.adapter.close()
            self.adapter = BilibiliMessagesAdapter(credentials)
            self.credential_signature = signature
        return self.adapter

    async def sync(self, *, automatic=False):
        self._enabled()
        if automatic and not self.policy()["auto_poll"]:
            raise ValueError("自动读取私信已停止")
        async with self.sync_lock:
            credential = dict(self.account.credentials)
            adapter = await self._adapter()

            def require_current_account():
                if not self.injected and (
                    self.adapter is not adapter
                    or self.account.credentials != credential
                    or self.credential_signature != (credential.get("sessdata"), credential.get("csrf"))
                ):
                    raise ValueError("读取期间账号已变更，未导入旧账号私信")

            require_current_account()
            own_uid, users = await adapter.sessions()
            require_current_account()
            self.active_account_id, self.active_credential = str(own_uid), credential
            if automatic:
                self._inbox_cutoff(str(own_uid))
            count, errors = 0, []
            for uid in users[:20]:
                if automatic and not self.operations.accepting_work:
                    break
                require_current_account()
                if not self.policy()["enabled"] or (automatic and not self.policy()["auto_poll"]):
                    break
                try:
                    known = self.store.one(
                        "SELECT display_name FROM users WHERE platform='bilibili' AND platform_user_id=?",
                        (uid,),
                    )
                    def require_read_permission():
                        require_current_account()
                        if not self.policy()["enabled"] or (automatic and not self.policy()["auto_poll"]):
                            raise ValueError("私信读取已停止")

                    imported, _ = await self._sync_history(
                        adapter, uid, own_uid, known["display_name"] if known else f"UID {uid}",
                        require_read_permission, cutoff=self._inbox_cutoff(str(own_uid)) if automatic else None,
                        background=automatic,
                    )
                    count += imported
                except (ValueError, KeyError, TypeError) as error:
                    errors.append(f"UID {uid}：{public_error(error)}")
            require_current_account()
            if automatic and self.policy()["auto_poll"] and self.operations.accepting_work:
                self._queue_newest_messages(str(own_uid))
            self.last_sync = time.time()
            self.error = "；".join(errors)[:500] or None
            return {"imported": count, "errors": errors}

    async def _sync_history(
        self, adapter, uid, account, name, guard, *, cutoff=None, require_latest=False, background=False
    ):
        if not hasattr(adapter, "history_page"):
            rows = await adapter.history(uid)
            guard()
            return self._ingest_history(rows, uid, account, name), True
        scope = (str(account), self.runtime.character["id"], str(uid))
        state = self.store.one(
            "SELECT newest,before_sequence,pending_newest FROM dm_sync_cursors WHERE account_id=? AND character_id=? AND platform_user_id=?",
            scope,
        ) or {"newest": 0, "before_sequence": 0, "pending_newest": 0}
        newest, before, pending = state["newest"], state["before_sequence"], state["pending_newest"]
        refreshed_head = not before
        count = 0
        for _ in range(3):
            if background and not self.operations.accepting_work:
                return count, False
            rows, next_page, latest = await adapter.history_page(uid, before=before)
            guard()
            if self.runtime.character["id"] != scope[1]:
                raise ValueError("读取期间角色已变化")
            count += self._ingest_history(rows, uid, account, name)
            pending = max(pending, latest)
            before = next_page
            if before and (
                (newest and before <= newest)
                or (cutoff is not None and any(float(row.get("timestamp", 0)) < cutoff for row in rows))
            ):
                before = 0
            self.store.execute(
                "INSERT OR REPLACE INTO dm_sync_cursors VALUES(?,?,?,?,?,?)",
                (
                    *scope,
                    newest if before else pending,
                    before,
                    pending if before else 0,
                ),
            )
            if not before and (refreshed_head or not require_latest):
                return count, True
            if not before:
                newest, pending = pending, 0
                refreshed_head = True
        return count, False

    async def _refresh_conversation_inbox(self, draft):
        if self.injected or not hasattr(await self._adapter(), "history"):
            return True
        if self.runtime.character["id"] != draft["character_id"]:
            raise ValueError("当前OC已变化，请切回原会话角色")
        async with self.sync_lock:
            credential = dict(self.account.credentials)
            adapter = await self._adapter()
            account = str(await adapter.self_uid())
            self._require_unchanged_draft(draft)
            if account != draft["account_id"]:
                raise ValueError("当前账号与私信会话不一致")
            def guard():
                self._require_unchanged_draft(draft)
                if self.adapter is not adapter or credential != self.account.credentials:
                    raise ValueError("读取期间账号已变化，未混入私密上下文")

            _, complete = await self._sync_history(adapter, draft["platform_user_id"], account, draft["display_name"],
                guard, cutoff=self._inbox_cutoff(account), require_latest=True)
            return complete

    async def _poll(self):
        poll_at = 0.0
        while True:
            self.changed.clear()
            policy = self.policy()
            if not self.operations.accepting_work or not policy["auto_poll"]:
                poll_at = 0.0
            if self.operations.accepting_work and policy["enabled"] and policy["auto_poll"] and time.monotonic() >= poll_at:
                poll_at = time.monotonic() + policy["poll_interval_seconds"]
                try:
                    async with self.operations.automatic_work() as admitted:
                        if admitted:
                            await self.sync(automatic=True)
                except Exception as error:
                    self.error = public_error(error)
            if policy["enabled"]:
                try:
                    await self._resume_pending()
                except Exception as error:
                    self.error = public_error(error)
            delay = self.policy()["poll_interval_seconds"]
            if poll_at > time.monotonic():
                delay = min(delay, poll_at - time.monotonic())
            if policy["enabled"] and self.operations.accepting_work:
                control = self.operations.policy()
                account, character, version = (control[key] for key in (
                    "account_id", "character_id", "character_version"
                ))
                pending = self.store.one(
                    "SELECT MIN(next_attempt_at) AS next_at FROM dm_drafts WHERE account_id=? AND character_id=? "
                    "AND character_version=? AND status='queued' AND resume_pending=1 AND next_attempt_at>?",
                    (account, character, version, time.time()),
                )
                if pending["next_at"] is not None:
                    delay = min(delay, max(0.05, pending["next_at"] - time.time()))
            try:
                await asyncio.wait_for(self.changed.wait(), delay)
            except TimeoutError:
                pass

    async def open(self):
        self.closed = False
        self.worker = asyncio.create_task(self._work())
        self.poller = asyncio.create_task(self._poll())

    async def close(self):
        self.closed = True
        await self.pause()
        for task in (self.worker, self.poller):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (self.worker, self.poller) if task), return_exceptions=True)
        if self.adapter:
            await self.adapter.close()
