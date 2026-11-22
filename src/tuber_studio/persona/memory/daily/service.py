"""Source-backed long-term memories and stored community highlights facade."""

from __future__ import annotations

from typing import Any

from .. import sources as memory_sources
from .organizer_bridge import DailyMemoryOrganizerBridge
from .repository import DailyMemoryRepository
from .scoring import extract_recall_keywords, is_retrospective_query, score_highlight_match


class DailyMemoryService:
    def __init__(self, store):
        self.store = store
        self._repo = DailyMemoryRepository(store)
        self._bridge = DailyMemoryOrganizerBridge(self._repo)
        if store is not None:
            # Explicitly withdrawn public experiences share the legacy status. Only
            # promote automatically organized or source-verified distilled memories.
            memory_sources.migrate(self)

    @property
    def private_owner(self):
        return self._repo.private_owner

    @private_owner.setter
    def private_owner(self, value):
        self._repo.private_owner = value

    @staticmethod
    def is_retrospective_query(text: str) -> bool:
        return is_retrospective_query(text)

    @staticmethod
    def _extract_recall_keywords(query: str) -> set[str]:
        return extract_recall_keywords(query)

    @staticmethod
    def _score_highlight_match(row: dict[str, Any], keywords: set[str], query_text: str) -> int:
        return score_highlight_match(row, keywords, query_text)

    def _upgrade_pending(self):
        return self._repo._upgrade_pending()

    @staticmethod
    def _source_proof(kind, source_ref, quote):
        return DailyMemoryRepository._source_proof(kind, source_ref, quote)

    def _legacy_source_evidence(self, row):
        return self._repo._legacy_source_evidence(row)

    def _source_evidence(self, row):
        return self._repo._source_evidence(row)

    @staticmethod
    def _title_key(title):
        return DailyMemoryRepository._title_key(title)

    def preference(self, scope):
        return self._repo.preference(scope)

    def set_preference(self, scope, enabled):
        return self._repo.set_preference(scope, enabled)

    def source_deleted(self, scope, key, owner):
        return self._repo.source_deleted(scope, key, owner)

    def history_turn_available(self, scope, key, owner):
        return self._repo.history_turn_available(scope, key, owner)

    def _invalidate_previous_turn(self, row):
        return self._repo._invalidate_previous_turn(row)

    def _forget_sources(self, scope, row):
        return self._repo._forget_sources(scope, row)

    def record_public_incident(self, character_id, character_version, *, incident_key, highlight_date, headline, anecdote, public_sources, tags=None):
        return self._repo.record_public_incident(character_id, character_version, incident_key=incident_key, highlight_date=highlight_date, headline=headline, anecdote=anecdote, public_sources=public_sources, tags=tags)

    def context(self, character, query_text='', *, video_id=None, thread_id=None, comment_id=None):
        return self._repo.context(character, query_text, video_id=video_id, thread_id=thread_id, comment_id=comment_id)

    def public_experience_sources(self, character_id, character_version, *, material_query=None, bound_ids=()):
        return self._repo.public_experience_sources(character_id, character_version, material_query=material_query, bound_ids=bound_ids)

    def business_date(self, now):
        return self._repo.business_date(now)

    def public_incident_sources(self, character_id, character_version, *, material_query=None, bound_ids=()):
        return self._repo.public_incident_sources(character_id, character_version, material_query=material_query, bound_ids=bound_ids)

    def record_highlight(self, character_id: str, character_version: int, highlight_date: str, category: str, headline: str, quote: str, anecdote: str, *, source_ref: str='', source_user: str='', tags: list[str] | None=None, status: str='active', source_evidence: dict | None=None, operator_confirmed: bool=False) -> int:
        return self._repo.record_highlight(character_id, character_version, highlight_date, category, headline, quote, anecdote, source_ref=source_ref, source_user=source_user, tags=tags, status=status, source_evidence=source_evidence, operator_confirmed=operator_confirmed)

    def update_active_timestamp(self, current_time: float | None=None):
        return self._repo.update_active_timestamp(current_time)

    def check_and_record_downtime(self, character_id: str, character_version: int, current_time: float | None=None) -> dict | None:
        return self._repo.check_and_record_downtime(character_id, character_version, current_time)

    def get_active_anecdotes(self, character_id: str, character_version: int, *, query_text: str='', days: int=3, limit: int=2, max_tokens: int=160, include_incidents: bool=True) -> list[dict[str, Any]]:
        return self._repo.get_active_anecdotes(character_id, character_version, query_text=query_text, days=days, limit=limit, max_tokens=max_tokens, include_incidents=include_incidents)

    def recall_historical_highlight(self, character_id: str, character_version: int, query_text: str, *, exclude_ids: set[int] | None=None, max_chars: int=70, min_score: int=3) -> dict[str, Any] | None:
        return self._repo.recall_historical_highlight(character_id, character_version, query_text, exclude_ids=exclude_ids, max_chars=max_chars, min_score=min_score)

    def list_highlights(self, character_id: str, character_version: int, *, date_str: str | None=None, status: str | None=None, limit: int=50, account_id: str | None=None) -> list[dict[str, Any]]:
        return self._repo.list_highlights(character_id, character_version, date_str=date_str, status=status, limit=limit, account_id=account_id)

    def update_highlight(self, highlight_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        return self._repo.update_highlight(highlight_id, updates)

    def delete_highlight(self, highlight_id: int) -> bool:
        return self._repo.delete_highlight(highlight_id)

    def organized(self, scope, *, limit=100, item_id=None, unclassified=False, recall=None, exact_title=None, candidate_for=None, memory_kind=None, material_query=None):
        return self._bridge.organized(scope, limit=limit, item_id=item_id, unclassified=unclassified, recall=recall, exact_title=exact_title, candidate_for=candidate_for, memory_kind=memory_kind, material_query=material_query)

    @staticmethod
    def _public_memory_projection(proof, source):
        return DailyMemoryOrganizerBridge._public_memory_projection(proof, source)

    def summary(self, scope, *, organized=None):
        return self._bridge.summary(scope, organized=organized)

    @staticmethod
    def owner_projection(row):
        return DailyMemoryOrganizerBridge.owner_projection(row)

    @staticmethod
    def memory_visibility(row):
        return DailyMemoryOrganizerBridge.memory_visibility(row)

    def resolve_legacy_private(self, scope, row, decision, *, reviewed_chat=False, rule=None):
        return self._bridge.resolve_legacy_private(scope, row, decision, reviewed_chat=reviewed_chat, rule=rule)

    def save_organized(self, scope, source, decision, existing):
        return self._bridge.save_organized(scope, source, decision, existing)

    def _save_organized(self, scope, source, decision, existing):
        return self._bridge._save_organized(scope, source, decision, existing)

    def lookup_source(self, scope, channel, source_key):
        return self._bridge.lookup_source(scope, channel, source_key)

    def change_organized(self, scope, item_id, *, owner_only=None, headline=None, note=None, delete=False, expected_revision=None):
        return self._bridge.change_organized(scope, item_id, owner_only=owner_only, headline=headline, note=note, delete=delete, expected_revision=expected_revision)

    def _change_organized(self, scope, item_id, owner_only, headline, note, delete, expected_revision):
        return self._bridge._change_organized(scope, item_id, owner_only, headline, note, delete, expected_revision)

    def live_preferences(self, scope):
        return self._bridge.live_preferences(scope)

    def memory_match(self, row, text):
        return self._bridge.memory_match(row, text)

    def recall_organized(self, scope, channel, text, *, resource=None, thread=None, participant=None, specific_experiences=False, manual_inspection=False, memory_kind=None, limit=3):
        return self._bridge.recall_organized(scope, channel, text, resource=resource, thread=thread, participant=participant, specific_experiences=specific_experiences, manual_inspection=manual_inspection, memory_kind=memory_kind, limit=limit)

    def validate_organized(self, scope, channel, refs):
        return self._bridge.validate_organized(scope, channel, refs)

    @staticmethod
    def organized_reference(row):
        return DailyMemoryOrganizerBridge.organized_reference(row)
