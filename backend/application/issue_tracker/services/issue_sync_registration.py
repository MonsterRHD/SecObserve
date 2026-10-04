"""
Registration of the desired state of issue tracker synchronizations.

Business transactions only call the functions of this module. They upsert an
Issue_Tracker_Status row with the desired state (OPEN/CLOSED/DELETED) and a monotonically
increasing revision inside their own database transaction, and schedule a background
synchronization after commit. No remote system is contacted from here, and no snapshot of
the observation is handed to a task: the worker always loads the latest revision itself.
"""

import logging
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from application.core.models import Observation, Product
from application.core.types import Severity, Status
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services.issue_sync_executor import (
    schedule_observation_sync,
)
from application.issue_tracker.services.sync_constants import LOCK_TIMEOUT
from application.issue_tracker.types import Desired_State, Sync_State

logger = logging.getLogger("secobserve.issue_tracker")

RESYNC_CHUNK_SIZE = 500


def get_correlation_id(observation_id: int) -> str:
    return f"secobserveobservation{observation_id}"


def register_observation_sync(observation: Observation) -> Optional[Issue_Tracker_Status]:
    """
    Registers the current state of an observation as the desired state. Called inside the
    business transaction after the observation has been created or changed. Does nothing
    for products without an active issue tracker integration or observations not on the
    default branch, so that no tasks are created for irrelevant observations.
    """
    product = observation.product
    if not product.issue_tracker_active:
        return None
    if observation.branch_id != product.repository_default_branch_id:
        return None

    desired_state = compute_desired_state(observation)
    return _upsert_status_row(
        product=product,
        observation=observation,
        issue_id=observation.issue_tracker_issue_id,
        desired_state=desired_state,
    )


def register_observation_deleted(observation: Observation) -> Optional[Issue_Tracker_Status]:
    """
    Registers that the observation has been deleted. Must be called from a pre_delete
    signal in the same transaction: the status row survives the deletion with its product,
    correlation id and issue id (the observation foreign key is set to null), so that the
    deletion note and the close operation can be retried reliably. Rows of observations
    that have never been tracked are not registered.
    """
    product = observation.product
    if not product.issue_tracker_active:
        return None

    with transaction.atomic():
        try:
            status_row = Issue_Tracker_Status.objects.select_for_update().get(observation=observation)
        except Issue_Tracker_Status.DoesNotExist:
            if not observation.issue_tracker_issue_id:
                return None
            status_row = Issue_Tracker_Status(
                product=product,
                observation=observation,
                correlation_id=get_correlation_id(observation.pk),
                issue_id=observation.issue_tracker_issue_id,
                desired_state=Desired_State.DESIRED_STATE_DELETED,
                revision=1,
                sync_state=Sync_State.SYNC_STATE_PENDING,
                next_attempt_at=timezone.now(),
            )
            status_row.save()
        else:
            # R3-3: never blindly delete an in-flight row. A fresh lease is preserved, the
            # worker requeues itself when it sees the newer DELETED revision.
            _bump_locked_row(
                status_row,
                desired_state=Desired_State.DESIRED_STATE_DELETED,
                issue_id=observation.issue_tracker_issue_id,
            )
            status_row.save()

        schedule_observation_sync(status_row.id)
        return status_row


def close_all_for_deactivation(product: Product) -> int:
    """
    Desired state for all tracked observations becomes CLOSED when the issue tracker
    integration is deactivated. Rows with a fresh worker lease are bumped under the same
    lock rules instead of being deleted, so an issue that is created while the integration
    is switched off is closed in the following run instead of becoming an orphan.
    """
    now = timezone.now()
    scheduled_rows: list[int] = []

    with transaction.atomic():
        status_rows = list(
            Issue_Tracker_Status.objects.select_for_update()
            .filter(product=product)
            .exclude(desired_state=Desired_State.DESIRED_STATE_DELETED)
        )
        for status_row in status_rows:
            fresh_lease = _has_fresh_lease(status_row, now)
            _bump_locked_row(
                status_row,
                desired_state=Desired_State.DESIRED_STATE_CLOSED,
                issue_id=status_row.issue_id,
            )
            status_row.save()
            if not fresh_lease:
                scheduled_rows.append(status_row.id)

        handled_observation_ids = [row.observation_id for row in status_rows if row.observation_id]
        legacy_observations = list(
            Observation.objects.filter(product=product)
            .exclude(issue_tracker_issue_id="")
            .exclude(pk__in=handled_observation_ids)
        )
        new_rows = [
            Issue_Tracker_Status(
                product=product,
                observation=observation,
                correlation_id=get_correlation_id(observation.pk),
                issue_id=observation.issue_tracker_issue_id,
                desired_state=Desired_State.DESIRED_STATE_CLOSED,
                revision=1,
                sync_state=Sync_State.SYNC_STATE_PENDING,
                next_attempt_at=now,
            )
            for observation in legacy_observations
        ]
        if new_rows:
            Issue_Tracker_Status.objects.bulk_create(new_rows, ignore_conflicts=True)
            scheduled_rows.extend(row.id for row in new_rows)

    for status_row_id in scheduled_rows:
        schedule_observation_sync(status_row_id)

    return len(scheduled_rows)


def register_product_resync(product: Product) -> int:
    """
    Recomputes the desired state for all relevant observations of a product. Used when the
    integration is activated or when the default branch or the minimum severity changes:
    observations on the default branch are reconciled, issues of observations that no
    longer qualify are closed.
    """
    if not product.issue_tracker_active or product.is_product_group:
        return 0

    now = timezone.now()
    count = 0

    observations = (
        Observation.objects.filter(product=product)
        .filter(
            Q(branch=product.repository_default_branch)
            | ~Q(issue_tracker_issue_id="")
            | Q(issue_tracker_status__isnull=False)
        )
        .distinct()
    )

    with transaction.atomic():
        locked_rows = {
            row.observation_id: row
            for row in Issue_Tracker_Status.objects.select_for_update().filter(product=product)
            if row.observation_id
        }

        to_create: list[Issue_Tracker_Status] = []
        to_update: list[Issue_Tracker_Status] = []
        processed_observations: list[Observation] = []

        for observation in observations.iterator(chunk_size=RESYNC_CHUNK_SIZE):
            desired_state = compute_desired_state(observation)
            status_row = locked_rows.get(observation.pk)
            if status_row:
                if status_row.desired_state == Desired_State.DESIRED_STATE_DELETED:
                    continue
                _bump_locked_row(status_row, desired_state, observation.issue_tracker_issue_id)
                to_update.append(status_row)
            else:
                to_create.append(
                    Issue_Tracker_Status(
                        product=product,
                        observation=observation,
                        correlation_id=get_correlation_id(observation.pk),
                        issue_id=observation.issue_tracker_issue_id,
                        desired_state=desired_state,
                        revision=1,
                        sync_state=Sync_State.SYNC_STATE_PENDING,
                        next_attempt_at=now,
                    )
                )
            processed_observations.append(observation)

        if to_create:
            created_rows = Issue_Tracker_Status.objects.bulk_create(to_create, ignore_conflicts=True)
            count += len(created_rows)
        if to_update:
            Issue_Tracker_Status.objects.bulk_update(
                to_update,
                [
                    "revision",
                    "desired_state",
                    "issue_id",
                    "sync_state",
                    "locked_at",
                    "attempts",
                    "next_attempt_at",
                    "last_error",
                ],
            )
            count += len(to_update)

        scheduled_observations = [row.id for row in to_update if not _has_fresh_lease(row, now)]
        scheduled_creates = [row.id for row in to_create]

    for status_row_id in [*scheduled_observations, *scheduled_creates]:
        schedule_observation_sync(status_row_id)

    logger.debug("Registered issue tracker resync for %s observations of product %s", count, product)
    return count


def reset_for_manual_retry(status_row: Issue_Tracker_Status) -> bool:
    """
    Resets a FAILED row (or a PENDING row waiting for its backoff) for an immediate manual
    retry. The create_uncertain flag is kept, so the retry still searches by correlation
    marker before creating anything. A row with a fresh lease or a succeeded row is not
    touched.
    """
    with transaction.atomic():
        try:
            locked_row = Issue_Tracker_Status.objects.select_for_update().get(pk=status_row.pk)
        except Issue_Tracker_Status.DoesNotExist:
            return False

        if locked_row.sync_state not in (Sync_State.SYNC_STATE_FAILED, Sync_State.SYNC_STATE_PENDING):
            return False

        locked_row.revision += 1
        locked_row.sync_state = Sync_State.SYNC_STATE_PENDING
        locked_row.locked_at = None
        locked_row.attempts = 0
        locked_row.next_attempt_at = timezone.now()
        locked_row.last_error = ""
        locked_row.save()

        schedule_observation_sync(locked_row.id)
        return True


def compute_desired_state(observation: Observation) -> str:
    if (
        observation.branch == observation.product.repository_default_branch
        and observation.current_status in Status.STATUS_ACTIVE
    ):
        if observation.product.issue_tracker_minimum_severity:
            numerical_minimum_severity = Severity.NUMERICAL_SEVERITIES.get(
                observation.product.issue_tracker_minimum_severity, 99
            )
        else:
            numerical_minimum_severity = 99
        if observation.numerical_severity <= numerical_minimum_severity:
            return Desired_State.DESIRED_STATE_OPEN
    return Desired_State.DESIRED_STATE_CLOSED


def _upsert_status_row(
    *,
    product: Product,
    observation: Observation,
    issue_id: str,
    desired_state: str,
    status_row: Optional[Issue_Tracker_Status] = None,
) -> Optional[Issue_Tracker_Status]:
    # The savepoint makes the fallback of a concurrent first insert work on PostgreSQL as
    # well: a unique constraint violation there aborts the whole transaction otherwise.
    with transaction.atomic():
        if status_row is None:
            try:
                locked_row = Issue_Tracker_Status.objects.select_for_update().get(observation=observation)
            except Issue_Tracker_Status.DoesNotExist:
                try:
                    locked_row = Issue_Tracker_Status(
                        product=product,
                        observation=observation,
                        correlation_id=get_correlation_id(observation.pk),
                        issue_id=issue_id,
                        desired_state=desired_state,
                        revision=1,
                        sync_state=Sync_State.SYNC_STATE_PENDING,
                        next_attempt_at=timezone.now(),
                    )
                    locked_row.save()
                except IntegrityError:
                    # A concurrent transaction inserted the row first (visible on MySQL
                    # statement rollback or via this savepoint on PostgreSQL).
                    locked_row = Issue_Tracker_Status.objects.select_for_update().get(observation=observation)
                    _bump_locked_row(locked_row, desired_state, issue_id)
                    locked_row.save()
            else:
                _bump_locked_row(locked_row, desired_state, issue_id)
                locked_row.save()
        else:
            locked_row = Issue_Tracker_Status.objects.select_for_update().get(pk=status_row.pk)
            _bump_locked_row(locked_row, desired_state, issue_id)
            locked_row.save()

        schedule_observation_sync(locked_row.id)
        return locked_row


def _bump_status_row(status_row: Issue_Tracker_Status, desired_state: str, issue_id: str) -> Issue_Tracker_Status:
    """
    Applies a new desired revision to an existing row. Always re-reads the row with a lock
    first: on MySQL REPEATABLE READ a locking read is a current read, so a request that
    read the row before a worker's claim cannot wipe a fresh lease with a stale view.
    """
    with transaction.atomic():
        locked_row = Issue_Tracker_Status.objects.select_for_update().get(pk=status_row.pk)
        _bump_locked_row(locked_row, desired_state, issue_id)
        locked_row.save()
        return locked_row


def _bump_locked_row(status_row: Issue_Tracker_Status, desired_state: str, issue_id: str) -> None:
    now = timezone.now()
    status_row.revision += 1
    status_row.desired_state = desired_state
    if issue_id:
        # Never downgrade a known remote issue id with a stale hint.
        status_row.issue_id = issue_id

    if not _has_fresh_lease(status_row, now):
        status_row.sync_state = Sync_State.SYNC_STATE_PENDING
        status_row.locked_at = None
        status_row.attempts = 0
        status_row.next_attempt_at = now
        status_row.last_error = ""
    # With a fresh lease the row stays IN_PROGRESS with its lease and backoff; the running
    # worker sees the newer revision when it finishes and requeues exactly once.
    # create_uncertain is never reset here: it is only cleared after confirmed success.


def _has_fresh_lease(status_row: Issue_Tracker_Status, now) -> bool:
    return bool(
        status_row.sync_state == Sync_State.SYNC_STATE_IN_PROGRESS
        and status_row.locked_at
        and status_row.locked_at > now - LOCK_TIMEOUT
    )
