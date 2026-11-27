"""Conversation window ordering, incoming settlement tracking, and queued draft workers."""

import asyncio
import json
import time

from ....core.identity import runtime_identity
from ....core.models import content_hash
from ....infra.inference.providers import public_error
from .models import ConversationWindow, MessageInputChanged
from .outreach import confirmed_invitation, verified_agreement

_ROLE_LABELS = {"viewer": "对方", "oc": "当前角色已发送", "operator": "号主人工接管"}


class DirectMessagesWindowingMixin:
    def _queue_newest_messages(self, account_id):
        cutoff = self._inbox_cutoff(account_id)
        if not self._processing_authorized() or not self.operations.accepting_work:
            return
        policy = self.policy()
        browsing = getattr(self.runtime, "browsing", None)
        if policy["auto_send"] and browsing and browsing.sends.preparation_block(
            account_id, "messages", policy, retry_seconds=policy["poll_interval_seconds"],
        ):
            return
        slots = self.queue.maxsize - self.queue.qsize()
        if not slots:
            return
        candidates = self.store.all(
            """WITH newest AS (
                   SELECT id,platform,platform_user_id,sent_at,
                          MIN(received_at) OVER (PARTITION BY platform,platform_user_id) AS waiting_since,
                          ROW_NUMBER() OVER (PARTITION BY platform,platform_user_id ORDER BY sequence DESC,sent_at DESC,id DESC) AS rank
                   FROM dm_inbox i WHERE account_id=? AND character_id=? AND (sent_at>=? OR EXISTS(
                     SELECT 1 FROM dm_recovered_incoming r WHERE r.incoming_id=i.id))
                   AND NOT EXISTS (SELECT 1 FROM dm_incoming_settlements s WHERE s.incoming_id=i.id)
                   AND NOT EXISTS (SELECT 1 FROM dm_sync_cursors c WHERE c.account_id=i.account_id
                       AND c.character_id=i.character_id AND c.platform_user_id=i.platform_user_id AND c.before_sequence<>0)
               ) SELECT id,waiting_since FROM newest n WHERE rank=1 AND NOT EXISTS (
                   SELECT 1 FROM dm_drafts d WHERE d.account_id=? AND d.character_id=?
                   AND d.platform=n.platform AND d.platform_user_id=n.platform_user_id
                   AND ((d.source_kind='message' AND d.source_id=n.id) OR
                        (d.status IN ('queued','generating','publishing','publish_uncertain') AND d.character_version=?
                         AND (d.source_kind='message' OR d.status IN ('publishing','publish_uncertain'))))
               ) ORDER BY waiting_since,id LIMIT ?""",
            (
                account_id,
                self.runtime.character["id"],
                cutoff,
                account_id,
                self.runtime.character["id"],
                self.runtime.character["version"],
                slots,
            ),
        )
        for candidate in candidates:
            self.enqueue(
                "message", candidate["id"], automatic=policy["auto_send"], continuous=True,
                waiting_since=candidate["waiting_since"],
            )

    def _source(self, kind, identity, character):
        table = "dm_inbox" if kind == "message" else "comments" if kind == "comment" else None
        if table is None:
            raise ValueError("私信只能由对方来信或当前评论触发")
        row = self.store.one(f"SELECT * FROM {table} WHERE id=?", (identity,))
        if not row or not row.get("platform_user_id"):
            raise ValueError("私信缺少可确认的来源用户ID")
        if kind == "message" and row["character_id"] != character["id"]:
            raise ValueError("该来信属于其他OC，请切回对应角色")
        return row

    def _event(self, draft, character):
        source = self._source(draft["source_kind"], draft["source_id"], character)
        if source["platform"] != draft["platform"] or source["platform_user_id"] != draft["platform_user_id"]:
            raise ValueError("私信收件人与原始来源不一致，请重新准备草稿")
        if draft["source_kind"] == "message" and str(source.get("account_id") or "") != draft.get(
            "account_id", ""
        ):
            raise ValueError("来信账号与私信上下文范围不一致，请重新同步来源")
        event = {
            "event_id": f"dm:{draft['id']}",
            "text": source["content"],
            "platform": draft["platform"],
            "platform_user_id": draft["platform_user_id"],
            "display_name": draft["display_name"],
            "recipient": {"uid": draft["platform_user_id"], "name": draft["display_name"]},
            "self_actor": runtime_identity(self.runtime, character, draft.get("account_id")),
            "delivery_channel": "private_message",
            "account_id": draft.get("account_id", ""),
            "contact_type": "reply" if draft["source_kind"] == "message" else "outreach",
            "contact_purpose": draft.get("purpose") or "",
        }
        if draft.get("video_evidence"):
            event["video_evidence"] = json.loads(draft["video_evidence"])
        if draft["source_kind"] == "comment":
            event["agreement_id"] = draft["id"]
            trigger = json.loads(draft.get("trigger_source") or "{}")
            scope = self.comments.account_context_scope(
                source["video_id"], source["thread_id"], source["platform"], account_id=draft["account_id"]
            )
            event["public_source_thread"] = (
                event.get("video_evidence", {}).get("public_source_thread")
                or self.runtime.operation_context.history_for_model(scope, character)
            )
            invitation = confirmed_invitation(
                event["public_source_thread"], draft.get("account_id"), draft["platform_user_id"]
            )
            if invitation:
                event["contact_invitation"] = invitation
            if trigger.get("contact_trigger") == "agreement" and verified_agreement(draft, event["public_source_thread"]):
                event["agreement_task"] = {"agreement_id": draft["id"], "purpose": draft.get("purpose") or "",
                    "completion_evidence": self._completion_evidence(draft),
                    "task": "用查找结果接上原来的约定"}
        else:
            conversation = self._reply_window(draft, source)
            event["conversation"] = conversation.timeline
            event["conversation_sha256"] = conversation.fingerprint
        if len(json.dumps(event, ensure_ascii=False)) > 1_000_000:
            raise ValueError("来源上下文过大，保留原文；请整理该楼后重新准备私信")
        return event

    @staticmethod
    def _ordered_messages(rows):
        by_time = sorted(rows, key=lambda row: (row["sent_at"], row["id"]))
        sequenced = iter(sorted((row for row in rows if row["sequence"]), key=lambda row: row["sequence"]))
        return [next(sequenced) if row["sequence"] else row for row in by_time]

    def _reply_window(self, draft, source):
        scope = tuple(draft[key] for key in ("platform", "platform_user_id", "character_id", "account_id"))
        common = "platform=? AND platform_user_id=? AND character_id=? AND account_id=?"
        order = " ORDER BY sequence DESC,sent_at DESC,id DESC"
        incoming = self.store.all(
            "SELECT i.id,message_id,content,sent_at,sequence,s.incoming_id AS settled,'viewer' AS origin "
            "FROM dm_inbox i LEFT JOIN dm_incoming_settlements s ON s.incoming_id=i.id WHERE "
            + common
            + " AND CASE WHEN sequence>0 AND ?>0 THEN sequence<=? ELSE (sent_at,i.id)<=(?,?) END"
            + order.replace("id DESC", "i.id DESC")
            + " LIMIT 30",
            (*scope, source["sequence"], source["sequence"], source["sent_at"], source["id"]),
        )
        outgoing = self._outgoing_history(draft)
        trigger = json.loads(draft.get("trigger_source") or "{}")
        cutoff = (
            self.store.get(f"message_inbox_cutoff:{draft['account_id']}:{draft['character_id']}", 0)
            if trigger.get("continuous") and not self._recovered_incoming(source["id"])
            else 0
        )
        batch_start = max(source["sent_at"] - 600, cutoff)
        claimed = {
            item["source_id"]
            for item in self.store.all(
                "SELECT source_id FROM dm_drafts WHERE " + common + " AND id<>? AND source_kind='message' "
                "AND status IN ('queued','generating','published','publishing','publish_uncertain',"
                "'rejected','copied','pending_review','approved')",
                (*scope, draft["id"]),
            )
        }
        pending = [
            item
            for item in incoming
            if not item["settled"] and item["id"] not in claimed and item["sent_at"] >= batch_start
        ][:8]

        # An equal timestamp without two sequence values cannot prove which turn was first.
        def later(response, message):
            if response["sequence"] and message["sequence"]:
                return response["sequence"] > message["sequence"]
            return response["sent_at"] > message["sent_at"]

        timeline = self._ordered_messages([*incoming, *outgoing])
        refs, links, public = {}, {}, []
        pending_ids = {message["id"] for message in pending}
        for index, item in enumerate(timeline, 1):
            ref = {
                "message": index,
                "speaker": item["origin"],
                "role_label": _ROLE_LABELS.get(item["origin"], item["origin"]),
                "text": item["content"],
            }
            if item["origin"] == "viewer" and item["id"] in pending_ids:
                refs[index] = item["id"]
            public.append(ref)
        for index, response in enumerate(timeline, 1):
            if response["origin"] != "operator":
                continue
            for i in refs:
                incoming = timeline[i - 1]
                if later(response, incoming):
                    links[(i, index)] = response["message_id"]
                elif response["sent_at"] == incoming["sent_at"] and not (
                    response["sequence"] and incoming["sequence"]
                ):
                    links[(i, index)] = None
        ambiguous = any(value is None for value in links.values())
        prior_settled = []
        unanswered_refs = {}
        for i, incoming_id in refs.items():
            matched_out = [
                (out_i, msg_id)
                for (in_i, out_i), msg_id in links.items()
                if in_i == i and msg_id is not None
            ]
            if matched_out:
                _, outgoing_msg_id = max(matched_out, key=lambda pair: pair[0])
                prior_settled.append((incoming_id, outgoing_msg_id))
            else:
                unanswered_refs[i] = incoming_id
                public[i - 1]["open"] = True
        latest_outgoing_id = None
        if not ambiguous and not unanswered_refs and prior_settled:
            latest_outgoing_id = prior_settled[-1][1]
        fingerprint = content_hash(
            [
                (message["message_id"], message["sequence"], view)
                for message, view in zip(timeline, public, strict=True)
            ]
        )
        return ConversationWindow(
            public,
            refs,
            links,
            fingerprint,
            latest_outgoing_id=latest_outgoing_id,
            unanswered_refs=unanswered_refs,
            prior_settled=prior_settled,
            ambiguous=ambiguous,
        )

    def _settle_incoming(self, draft, incoming_ids, outgoing_id):
        self.store.db.executemany(
            "INSERT OR IGNORE INTO dm_incoming_settlements SELECT id,? FROM dm_inbox "
            "WHERE id=? AND account_id=? AND character_id=? AND platform=? AND platform_user_id=?",
            [
                (
                    str(outgoing_id),
                    value,
                    draft["account_id"],
                    draft["character_id"],
                    draft["platform"],
                    draft["platform_user_id"],
                )
                for value in incoming_ids
            ],
        )

    def _recovered_incoming(self, incoming_id):
        return self.store.one("SELECT 1 FROM dm_recovered_incoming WHERE incoming_id=?", (incoming_id,)) is not None

    def _conversation_matches(self, draft, expected):
        if draft["source_kind"] != "message":
            return True
        source = self.store.one("SELECT * FROM dm_inbox WHERE id=?", (draft["source_id"],))
        conversation = self._reply_window(draft, source)
        return bool(conversation.refs) and (
            conversation.fingerprint == expected if expected else not conversation.links
        )

    def _requeue_changed_conversation(self, draft, automatic):
        self.store.execute(
            "UPDATE dm_drafts SET status='queued',resume_pending=1,next_attempt_at=0,error=NULL,updated_at=? "
            "WHERE id=? AND status NOT IN ('published','publish_uncertain','rejected')",
            (time.time(), draft["id"]),
        )
        self.authorizations[draft["id"]] = self.operations.policy()["epoch"] if automatic else None

    def _check_conversation(self, draft, expected, *, automatic):
        if not self._conversation_matches(draft, expected):
            self._requeue_changed_conversation(draft, automatic)
            raise MessageInputChanged("会话出现新发言，已保留原稿并准备更新回复")

    def _newer_incoming(self, draft, source):
        return self.store.one(
            "SELECT i.id,i.message_id FROM dm_inbox i WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? "
            "AND CASE WHEN sequence>0 AND ?>0 THEN sequence>? ELSE (sent_at,i.id)>(?,?) END "
            "AND NOT EXISTS (SELECT 1 FROM dm_incoming_settlements s WHERE s.incoming_id=i.id) "
            "ORDER BY sequence DESC,sent_at DESC,id DESC LIMIT 1",
            (draft["account_id"], draft["character_id"], draft["platform"], draft["platform_user_id"],
             source["sequence"], source["sequence"], source["sent_at"], source["id"]),
        )

    def _merge_incoming(self, identity, *, automatic=False):
        row = self.store.one("SELECT * FROM dm_drafts WHERE id=?", (identity,))
        if not row or row["status"] in {"published", "publishing", "publish_uncertain"}:
            return False
        if row["source_kind"] == "comment":
            incoming = self.store.one(
                "SELECT id FROM dm_inbox WHERE account_id=? AND character_id=? AND platform=? AND platform_user_id=? "
                "AND received_at>=? AND sent_at>=? ORDER BY sent_at DESC,id DESC LIMIT 1",
                (row["account_id"], row["character_id"], row["platform"], row["platform_user_id"], row["created_at"], int(row["created_at"])),
            )
            if incoming:
                reply = self.enqueue("message", incoming["id"], automatic=automatic,
                                     character=self.store.character(row["character_id"], row["character_version"]))
                self.store.execute("UPDATE dm_drafts SET status='waiting',resume_pending=0,error=NULL,outreach_check=? WHERE id=?",
                    (json.dumps({"action": "reply_received", "state": "reply_received", "reply_draft_id": reply}, ensure_ascii=False), identity))
                return True
            return False
        if row["source_kind"] != "message":
            return False
        source = self._source("message", row["source_id"], self.store.character(row["character_id"], row["character_version"]))
        latest = self._newer_incoming(row, source)
        if not latest:
            return False
        other = self.store.one("SELECT id FROM dm_drafts WHERE account_id=? AND character_id=? AND source_kind='message' AND source_id=? AND id<>?",
                               (row["account_id"], row["character_id"], latest["id"], identity))
        if other:
            self.store.execute("UPDATE dm_drafts SET status='cancelled',resume_pending=0,error=?,updated_at=? WHERE id=?",
                               ("新来信已合并到私信草稿 #" + str(other["id"]), time.time(), identity))
            return True
        trigger = json.loads(row.get("trigger_source") or "{}")
        trigger["ids"] = list(dict.fromkeys([*trigger.get("ids", [row["source_id"]]), latest["id"]]))
        trigger["message_id"] = latest["message_id"]
        self.store.execute("UPDATE dm_drafts SET source_id=?,trigger_source=?,status='queued',resume_pending=1,error=NULL,updated_at=? WHERE id=?",
                           (latest["id"], json.dumps(trigger, ensure_ascii=False), time.time(), identity))
        self.authorizations[identity] = self.operations.policy()["epoch"] if automatic else None
        return True

    async def _work(self):
        while True:
            _, _, identity, automatic = await self.queue.get()
            managed = automatic
            try:
                queued = self.store.one("SELECT resume_pending,trigger_source FROM dm_drafts WHERE id=?", (identity,))
                managed = automatic or bool(queued and (
                    queued["resume_pending"] or json.loads(queued["trigger_source"] or "{}").get("continuous")
                ))
                if managed:
                    async with self.operations.automatic_work() as admitted:
                        if admitted:
                            await self._generate(identity, automatic)
                        else:
                            self.store.execute("UPDATE dm_drafts SET resume_pending=1 WHERE id=? AND status='queued'", (identity,))
                else:
                    await self._generate(identity, automatic)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = public_error(error)
            finally:
                self._scheduled.discard(identity)
                self.queue.task_done()
                if not self.closed:
                    try:
                        retained = self.store.one("SELECT status,resume_pending,next_attempt_at FROM dm_drafts WHERE id=?", (identity,))
                        can_resume = not managed or self.operations.accepting_work and self._processing_authorized()
                        if (can_resume and retained and retained["status"] == "queued"
                                and retained["resume_pending"] and retained["next_attempt_at"] <= time.time()):
                            self._schedule(identity, automatic)
                        await self._resume_pending()
                    except Exception as error:
                        self.error = public_error(error)
