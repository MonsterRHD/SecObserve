"""
Scheduling of issue tracker synchronizations.

The outbox table in the application database is the source of truth: Huey tasks are only
a trigger. A periodic dispatch scans for due rows, so unconfirmed synchronizations still
run after a process restart even when their Huey message was lost.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services.issue_sync_executor import (
    trigger_observation_sync,
)
from application.issue_tracker.services.sync_constants import LOCK_TIMEOUT
from application.issue_tracker.types import Sync_State

logger = logging.getLogger("secobserve.issue_tracker")

DISPATCH_BATCH_SIZE = 100


def recover_after_restart() -> int:
    """
    Requeues rows that were left IN_PROGRESS by a killed worker (container restart).
    Called at startup and before every dispatch.
    """
    cutoff = timezone.now() - LOCK_TIMEOUT
    recovered = Issue_Tracker_Status.objects.filter(
        sync_state=Sync_State.SYNC_STATE_IN_PROGRESS,
        locked_at__lte=cutoff,
    ).update(
        sync_state=Sync_State.SYNC_STATE_PENDING,
        locked_at=None,
        next_attempt_at=timezone.now(),
    )
    if recovered:
        logger.info("Recovered %s issue tracker synchronizations after restart", recovered)
    return recovered


def dispatch_pending(limit: int = DISPATCH_BATCH_SIZE) -> str:
    recovered = recover_after_restart()

    now = timezone.now()
    due_status_row_ids = list(
        Issue_Tracker_Status.objects.filter(sync_state=Sync_State.SYNC_STATE_PENDING)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .order_by("next_attempt_at", "id")
        .values_list("id", flat=True)[:limit]
    )

    for status_row_id in due_status_row_ids:
        trigger_observation_sync(status_row_id)

    failed = Issue_Tracker_Status.objects.filter(sync_state=Sync_State.SYNC_STATE_FAILED).count()

    return (
        f"Recovered {recovered}, dispatched {len(due_status_row_ids)} "
        f"issue tracker synchronizations, {failed} failed"
    )
