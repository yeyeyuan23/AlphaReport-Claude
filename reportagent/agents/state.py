from __future__ import annotations

from typing import TypedDict, Optional, Callable, Any

from reportagent.models.schemas import (
    UserCriteria,
    SearchResult,
    ClassifiedReport,
    StorageResult,
)


class AgentState(TypedDict, total=False):
    criteria: UserCriteria
    task_id: str

    raw_results: list[SearchResult]
    collection_status: str
    collection_errors: list[str]

    classified_reports: list[ClassifiedReport]
    classification_status: str

    analysis_status: str

    storage_result: Optional[StorageResult]
    storage_status: str

    current_phase: str
    messages: list[str]

    _cancel_check: Callable[[], bool]
