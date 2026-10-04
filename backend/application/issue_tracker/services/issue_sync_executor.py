"""
Converges one Issue_Tracker_Status row against the remote issue tracker.

A worker first claims the row with a short database lease (SELECT ... FOR UPDATE
SKIP LOCKED), then talks to the remote system OUTSIDE of any long transaction. The
remote state is always queried before it is changed: after an uncertain create response
the issue is searched by its correlation marker, and close/reopen transitions are
skipped when the issue already has the target state. All database writes happen in short
finalize transactions which compare the revision again, so that a revision registered
while the worker was running causes exactly one requeue and stale tasks never overwrite
a newer result.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone
from huey.contrib.djhuey import db_task

from application.core.models import Observation
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    Issue,
    Issue_State,
    IssueTrackerDeterministicError,
    IssueTrackerUncertainError,
)
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services.sync_constants import (
    LOCK_TIMEOUT,
    get_correlation_id,
)
from application.issue_tracker.services.tracker_factory import issue_tracker_factory
from application.issue_tracker.types import Desired_State, Issue_Tracker, Sync_State
from application.notifications.services.tasks import handle_task_exception

logger = logging.getLogger("secobserve.issue_tracker")

MAX_ATTEMPTS = 10
# First retry after 1 minute, exponential backoff up to 60 minutes.
BACKOFF_DELTA_MINUTES = [1, 2, 4, 8, 16, 32, 60, 60, 60]
LAST_ERROR_MAX_LENGTH = 1024


@db_task()
def trigger_observation_sync(status_row_id: int) -> None:
    execute_sync(status_row_id)


def schedule_observation_sync(status_row_id: int, eta: Optional[datetime] = None) -> None:
    def enqueue() -> None:
        if eta is not None and eta > timezone.now():
            trigger_observation_sync.schedule(args=(status_row_id,), eta=eta)
        else:
            trigger_observation_sync(status_row_id)

    transaction.on_commit(enqueue)


class _ConvergenceResult:
    def __init__(self) -> None:
        self.terminate_row = False  # terminal DELETED row or canceled inactive row
        self.identity_changed = False  # a (possibly new) remote issue id was adopted
        self.observation_issue_id: Any = None  # None = do not mirror, "" = clear mirror
        self.observation_closed: Optional[bool] = None
        self.observation_jira_initial_status: Any = None

    def adopt_issue_id(self, issue_id: str) -> None:
        self.observation_issue_id = issue_id
        self.identity_changed = True


def execute_sync(status_row_id: int) -> None:
    status_row = _claim_status_row(status_row_id)
    if status_row is None:
        return

    claimed_revision = status_row.revision
    result = _ConvergenceResult()
    try:
        _converge(status_row, result)
    except IssueTrackerUncertainError as error:
        _handle_uncertain_error(status_row, claimed_revision, error)
        return
    except IssueTrackerDeterministicError as error:
        _handle_deterministic_error(status_row, claimed_revision, error)
        return
    except Exception as error:  # pylint: disable=broad-except
        # Never let an unexpected exception escape: persist it as a terminal failure so
        # the batch dispatcher continues and the failure stays observable.
        wrapped = IssueTrackerDeterministicError(str(error))
        logger.exception("Issue tracker synchronization %s failed with an unexpected error", status_row_id)
        _handle_deterministic_error(status_row, claimed_revision, wrapped)
        return

    _finish_success(status_row, claimed_revision, result)


def _claim_status_row(status_row_id: int) -> Optional[Issue_Tracker_Status]:
    """
    Claims a due row with a lease. Fresh IN_PROGRESS leases win over everything, even a
    future next_attempt_at. A stale lease (worker crashed) can always be claimed.
    """
    now = timezone.now()
    with transaction.atomic():
        try:
            status_row = (
                Issue_Tracker_Status.objects.select_for_update(skip_locked=True)
                .select_related("product", "observation", "observation__branch")
                .get(pk=status_row_id)
            )
        except Issue_Tracker_Status.DoesNotExist:
            return None

        if status_row.sync_state == Sync_State.SYNC_STATE_IN_PROGRESS:
            if status_row.locked_at and status_row.locked_at > now - LOCK_TIMEOUT:
                return None
        elif status_row.sync_state != Sync_State.SYNC_STATE_PENDING:
            return None
        elif status_row.next_attempt_at and status_row.next_attempt_at > now:
            return None

        status_row.sync_state = Sync_State.SYNC_STATE_IN_PROGRESS
        status_row.locked_at = now
        status_row.last_attempt_at = now
        status_row.attempts += 1
        status_row.save(update_fields=["sync_state", "locked_at", "last_attempt_at", "attempts", "modified"])
        return status_row


def _converge(status_row: Issue_Tracker_Status, result: _ConvergenceResult) -> None:
    product = status_row.product
    observation = status_row.observation
    tracker = issue_tracker_factory(product)

    issue = _load_remote_issue(status_row, product, tracker, result)

    if status_row.desired_state == Desired_State.DESIRED_STATE_DELETED:
        _converge_deleted(status_row, tracker, issue, result)
        return

    if not product.issue_tracker_active:
        _converge_inactive(status_row, observation, tracker, issue, result)
        return

    if status_row.desired_state == Desired_State.DESIRED_STATE_OPEN:
        _converge_open(status_row, observation, tracker, issue, result)
    else:
        _converge_closed(status_row, observation, tracker, issue, result)


def _load_remote_issue(
    status_row: Issue_Tracker_Status,
    product,
    tracker,
    result: _ConvergenceResult,
) -> Optional[Issue]:
    issue: Optional[Issue] = None

    if status_row.issue_id:
        issue = tracker.get_issue(product, status_row.issue_id)
        if issue is None:
            # The remote issue is gone. Clear the local identity, it will be recreated if
            # the desired state is OPEN.
            status_row.issue_id = ""
            result.observation_issue_id = ""

    # After an uncertain create response the issue may exist remotely although the local
    # issue id is missing. Search by the correlation marker BEFORE creating anything.
    # This runs for every desired state (OPEN/CLOSED/DELETED) and while the integration
    # is inactive.
    if not status_row.issue_id and status_row.create_uncertain:
        correlation_id = status_row.correlation_id or get_correlation_id(status_row.observation_id)
        found_issue_id = tracker.find_issue_id_by_correlation_id(product, correlation_id)
        if found_issue_id:
            issue = tracker.get_issue(product, found_issue_id)
            if issue is not None:
                status_row.issue_id = issue.id
                status_row.create_uncertain = False
                result.adopt_issue_id(issue.id)

    return issue


def _converge_open(status_row, observation, tracker, issue: Optional[Issue], result) -> None:
    product = status_row.product
    if not issue:
        new_issue_id = tracker.create_issue(observation)
        status_row.issue_id = new_issue_id
        status_row.create_uncertain = False
        result.adopt_issue_id(new_issue_id)
        result.observation_closed = False

        if product.issue_tracker_type == Issue_Tracker.ISSUE_TRACKER_JIRA:
            created_issue = tracker.get_issue(product, new_issue_id)
            if created_issue and created_issue.jira_initial_status:
                result.observation_jira_initial_status = created_issue.jira_initial_status
        return

    if issue.state == Issue_State.ISSUE_STATE_CLOSED:
        initial_status = tracker.update_issue(observation, issue)
        if initial_status and not observation.issue_tracker_jira_initial_status:
            result.observation_jira_initial_status = initial_status
    elif not _issue_is_current(tracker, product, observation, issue):
        tracker.update_issue(observation, issue)

    status_row.issue_id = issue.id
    status_row.create_uncertain = False
    result.observation_issue_id = issue.id
    result.observation_closed = False


def _converge_closed(status_row, observation, tracker, issue: Optional[Issue], result) -> None:
    if issue:
        if issue.state == Issue_State.ISSUE_STATE_OPEN:
            tracker.close_issue(observation, issue)
        status_row.issue_id = issue.id
        result.observation_issue_id = issue.id
    else:
        # No issue to close: either it never existed or it was confirmed absent.
        status_row.create_uncertain = False

    result.observation_closed = True


def _converge_deleted(status_row, tracker, issue: Optional[Issue], result) -> None:
    if issue:
        tracker.close_issue_for_deleted_observation(status_row.product, issue)
        status_row.issue_id = issue.id
    else:
        status_row.create_uncertain = False
    result.terminate_row = True


def _converge_inactive(status_row, observation, tracker, issue: Optional[Issue], result) -> None:
    # The integration has been deactivated. Existing issues are closed once. Rows with an
    # uncertain create are reconciled by marker search (already done in _load_remote_issue):
    # a found issue is adopted and closed, a confirmed absence cancels the row.
    if issue:
        if issue.state == Issue_State.ISSUE_STATE_OPEN:
            tracker.close_issue(observation, issue)
        status_row.issue_id = issue.id
        status_row.create_uncertain = False
        result.observation_issue_id = issue.id
        result.observation_closed = True
        return

    status_row.create_uncertain = False
    result.terminate_row = True


def _issue_is_current(tracker, product, observation, issue: Issue) -> bool:
    if issue.title != tracker._get_title(observation):  # pylint: disable=protected-access
        return False
    if product.issue_tracker_labels:
        required_labels = {item.strip() for item in product.issue_tracker_labels.split(",") if item.strip()}
        actual_labels = {item for item in (issue.labels or "").split(",") if item}
        if not required_labels.issubset(actual_labels):
            return False
    return True


def _finish_success(
    status_row: Issue_Tracker_Status,
    claimed_revision: int,
    result: _ConvergenceResult,
) -> None:
    with transaction.atomic():
        try:
            fresh_row = Issue_Tracker_Status.objects.select_for_update().get(pk=status_row.pk)
        except Issue_Tracker_Status.DoesNotExist:
            return

        if fresh_row.revision != claimed_revision:
            # A newer revision was registered while the remote calls were in flight.
            # Persist the adopted identity, then requeue exactly once.
            fresh_row.issue_id = status_row.issue_id
            fresh_row.create_uncertain = status_row.create_uncertain
            _reset_to_pending(fresh_row)
            fresh_row.save()
            _mirror_observation(fresh_row, result, identity_only=True)
            schedule_observation_sync(fresh_row.id)
            return

        fresh_row.issue_id = status_row.issue_id
        fresh_row.create_uncertain = status_row.create_uncertain
        fresh_row.sync_state = Sync_State.SYNC_STATE_SUCCEEDED
        fresh_row.locked_at = None
        fresh_row.attempts = 0
        fresh_row.next_attempt_at = None
        fresh_row.last_error = ""

        if result.terminate_row:
            # DELETED rows are terminal and rows for canceled inactive observations have
            # no remaining purpose.
            fresh_row.delete()
            return

        fresh_row.save()
        _mirror_observation(fresh_row, result, identity_only=False)


def _handle_uncertain_error(
    status_row: Issue_Tracker_Status,
    claimed_revision: int,
    error: IssueTrackerUncertainError,
) -> None:
    with transaction.atomic():
        try:
            fresh_row = Issue_Tracker_Status.objects.select_for_update().get(pk=status_row.pk)
        except Issue_Tracker_Status.DoesNotExist:
            return

        fresh_row.create_uncertain = True
        fresh_row.locked_at = None
        fresh_row.last_error = str(error)[:LAST_ERROR_MAX_LENGTH]

        if fresh_row.revision != claimed_revision:
            # Do not delay a newer revision for the whole backoff interval.
            _reset_to_pending(fresh_row)
            fresh_row.save()
            schedule_observation_sync(fresh_row.id)
            return

        if fresh_row.attempts >= MAX_ATTEMPTS:
            fresh_row.sync_state = Sync_State.SYNC_STATE_FAILED
            fresh_row.next_attempt_at = None
            fresh_row.save()
        else:
            eta = timezone.now() + _backoff_delta(fresh_row.attempts)
            fresh_row.sync_state = Sync_State.SYNC_STATE_PENDING
            fresh_row.next_attempt_at = eta
            fresh_row.save()
            schedule_observation_sync(fresh_row.id, eta=eta)


def _handle_deterministic_error(
    status_row: Issue_Tracker_Status,
    claimed_revision: int,
    error: IssueTrackerDeterministicError,
) -> None:
    with transaction.atomic():
        try:
            fresh_row = Issue_Tracker_Status.objects.select_for_update().get(pk=status_row.pk)
        except Issue_Tracker_Status.DoesNotExist:
            return

        fresh_row.locked_at = None
        fresh_row.last_error = str(error)[:LAST_ERROR_MAX_LENGTH]

        if fresh_row.revision != claimed_revision:
            # A newer revision may render the error obsolete, requeue immediately.
            _reset_to_pending(fresh_row)
            fresh_row.save()
            schedule_observation_sync(fresh_row.id)
            return

        fresh_row.sync_state = Sync_State.SYNC_STATE_FAILED
        fresh_row.next_attempt_at = None
        fresh_row.save()

    logger.error("Issue tracker synchronization %s failed deterministically: %s", status_row.pk, error)
    handle_task_exception(error, user=None, product=status_row.product)


def _reset_to_pending(status_row: Issue_Tracker_Status) -> None:
    status_row.sync_state = Sync_State.SYNC_STATE_PENDING
    status_row.locked_at = None
    status_row.attempts = 0
    status_row.next_attempt_at = timezone.now()
    status_row.last_error = ""


def _backoff_delta(attempts: int) -> timedelta:
    index = min(attempts - 1, len(BACKOFF_DELTA_MINUTES) - 1)
    return timedelta(minutes=BACKOFF_DELTA_MINUTES[index])


def _mirror_observation(
    status_row: Issue_Tracker_Status,
    result: _ConvergenceResult,
    *,
    identity_only: bool,
) -> None:
    observation_id = status_row.observation_id
    if not observation_id:
        return
    # R3-3: check the existence in the database, a cached observation object may still be
    # present in memory after the observation has been deleted.
    if not Observation.objects.filter(pk=observation_id).exists():
        return

    updates: dict[str, Any] = {}
    if result.observation_issue_id is not None:
        updates["issue_tracker_issue_id"] = result.observation_issue_id
    if result.observation_jira_initial_status is not None:
        updates["issue_tracker_jira_initial_status"] = result.observation_jira_initial_status
    if not identity_only and result.observation_closed is not None:
        updates["issue_tracker_issue_closed"] = result.observation_closed

    if updates:
        Observation.objects.filter(pk=observation_id).update(**updates)
