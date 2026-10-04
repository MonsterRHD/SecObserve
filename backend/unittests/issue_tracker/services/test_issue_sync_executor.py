from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.utils import timezone

from application.core.models import Branch, Observation, Product
from application.core.types import Severity, Status
from application.import_observations.models import Parser
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    Issue,
    Issue_State,
    IssueTrackerDeterministicError,
    IssueTrackerUncertainError,
)
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services import issue_sync_executor
from application.issue_tracker.services.issue_sync_dispatcher import dispatch_pending
from application.issue_tracker.services.issue_sync_registration import (
    close_all_for_deactivation,
    register_observation_deleted,
    register_observation_sync,
)
from application.issue_tracker.types import Desired_State, Issue_Tracker, Sync_State
from unittests.base_test_case import BaseTestCase

TITLE = "observation_title"


class MagicTracker:
    def __init__(self) -> None:
        self.remote_issues: dict[str, Issue] = {}
        self.marker_found_id: str | None = None
        self.find_calls = 0
        self.close_calls = 0
        self.deleted_close_calls = 0
        self.create_count = 0
        self.update_calls = 0
        self.created_issue_id = "gh_new"
        self.create_side_effect = None
        self._get_title = MagicMock(return_value=TITLE)
        self.initial_status_after_create: str | None = None

    def get_issue(self, product: Product, issue_id: str) -> Issue | None:
        return self.remote_issues.get(issue_id)

    def find_issue_id_by_correlation_id(self, product: Product, correlation_id: str) -> str | None:
        self.find_calls += 1
        return self.marker_found_id

    def create_issue(self, observation: Observation) -> str:
        self.create_count += 1
        if self.create_side_effect is not None:
            return self.create_side_effect(observation)
        return self.created_issue_id

    def update_issue(self, observation: Observation, issue: Issue) -> None:
        self.update_calls += 1

    def close_issue(self, observation: Observation, issue: Issue) -> None:
        self.close_calls += 1

    def close_issue_for_deleted_observation(self, product: Product, issue: Issue) -> None:
        self.deleted_close_calls += 1


class IssueSyncExecutorTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parser = Parser.objects.create(name="exec_parser")
        self.product = Product.objects.create(
            name="exec_product",
            issue_tracker_active=True,
            issue_tracker_type=Issue_Tracker.ISSUE_TRACKER_GITHUB,
            issue_tracker_project_id="org/repo",
            issue_tracker_status_closed="Closed",
        )
        self.branch = Branch.objects.create(name="main", product=self.product, is_default_branch=True)
        self.product.repository_default_branch = self.branch
        self.product.save()

        self.tracker = MagicTracker()
        factory_patcher = patch(
            "application.issue_tracker.services.issue_sync_executor.issue_tracker_factory",
            return_value=self.tracker,
        )
        self.factory_mock = factory_patcher.start()
        self.addCleanup(factory_patcher.stop)
        schedule_patcher = patch("application.issue_tracker.services.issue_sync_executor.schedule_observation_sync")
        self.schedule_mock = schedule_patcher.start()
        self.addCleanup(schedule_patcher.stop)
        notify_patcher = patch("application.issue_tracker.services.issue_sync_executor.handle_task_exception")
        self.notify_mock = notify_patcher.start()
        self.addCleanup(notify_patcher.stop)

    def _create_observation(self, *, status: str = Status.STATUS_OPEN, issue_id: str = "") -> Observation:
        observation = Observation.objects.create(
            title="observation",
            product=self.product,
            parser=self.parser,
            branch=self.branch,
            parser_status=status,
            parser_severity=Severity.SEVERITY_HIGH,
            import_last_seen=timezone.now(),
            issue_tracker_issue_id=issue_id,
        )
        return observation

    def _create_status_row(
        self,
        observation: Observation,
        *,
        desired_state: str = Desired_State.DESIRED_STATE_OPEN,
        issue_id: str = "",
        create_uncertain: bool = False,
        sync_state: str = Sync_State.SYNC_STATE_PENDING,
    ) -> Issue_Tracker_Status:
        return Issue_Tracker_Status.objects.create(
            product=self.product,
            observation=observation,
            correlation_id=f"secobserveobservation{observation.pk}",
            issue_id=issue_id,
            desired_state=desired_state,
            revision=1,
            sync_state=sync_state,
            create_uncertain=create_uncertain,
        )

    def _open_issue(self, issue_id: str = "gh_1", state: str = Issue_State.ISSUE_STATE_OPEN) -> Issue:
        return Issue(id=issue_id, title=TITLE, description="d", state=state)

    # --- OPEN -------------------------------------------------------------------------

    def test_open_without_issue_creates(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.create_count)
        observation.refresh_from_db()
        self.assertEqual("gh_new", observation.issue_tracker_issue_id)
        self.assertFalse(observation.issue_tracker_issue_closed)
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_SUCCEEDED, row.sync_state)
        self.assertFalse(row.create_uncertain)

    def test_open_with_current_open_issue_does_nothing_remotely(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)
        self.tracker.remote_issues = {"gh_1": self._open_issue()}

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(0, self.tracker.update_calls)
        self.assertEqual(0, self.tracker.create_count)
        observation.refresh_from_db()
        self.assertFalse(observation.issue_tracker_issue_closed)

    def test_open_with_closed_issue_reopens(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)
        self.tracker.remote_issues = {"gh_1": self._open_issue(state=Issue_State.ISSUE_STATE_CLOSED)}

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.update_calls)
        observation.refresh_from_db()
        self.assertFalse(observation.issue_tracker_issue_closed)

    def test_open_with_stale_title_updates(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)
        self.tracker.remote_issues = {"gh_1": Issue(id="gh_1", title="old title", description="d")}

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.update_calls)

    def test_label_drift_triggers_update(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        self.product.issue_tracker_labels = "label_a"
        self.product.save()
        row = register_observation_sync(observation)
        self.tracker.remote_issues = {"gh_1": Issue(id="gh_1", title=TITLE, description="d", labels="other_label")}

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.update_calls)

    def test_remote_issue_404_is_recreated_when_open_desired(self) -> None:
        observation = self._create_observation(issue_id="gh_gone")
        row = register_observation_sync(observation)

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.create_count)
        observation.refresh_from_db()
        self.assertEqual("gh_new", observation.issue_tracker_issue_id)

    # --- CLOSED -----------------------------------------------------------------------

    def test_closed_with_open_issue_closes_once(self) -> None:
        observation = self._create_observation(status=Status.STATUS_RESOLVED, issue_id="gh_1")
        row = register_observation_sync(observation)
        self.tracker.remote_issues = {"gh_1": self._open_issue()}

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.close_calls)
        observation.refresh_from_db()
        self.assertTrue(observation.issue_tracker_issue_closed)
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_SUCCEEDED, row.sync_state)

    def test_closed_with_already_closed_issue_does_not_transition(self) -> None:
        observation = self._create_observation(status=Status.STATUS_RESOLVED, issue_id="gh_1")
        row = register_observation_sync(observation)
        self.tracker.remote_issues = {"gh_1": self._open_issue(state=Issue_State.ISSUE_STATE_CLOSED)}

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(0, self.tracker.close_calls)
        observation.refresh_from_db()
        self.assertTrue(observation.issue_tracker_issue_closed)

    def test_closed_without_issue_succeeds_without_remote_call(self) -> None:
        observation = self._create_observation(status=Status.STATUS_RESOLVED)
        row = register_observation_sync(observation)

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(0, self.tracker.close_calls)
        self.assertEqual(0, self.tracker.create_count)
        observation.refresh_from_db()
        self.assertTrue(observation.issue_tracker_issue_closed)
        self.assertEqual("", observation.issue_tracker_issue_id)

    def test_remote_issue_404_when_closed_desired_clears_identity_and_succeeds(self) -> None:
        observation = self._create_observation(status=Status.STATUS_RESOLVED, issue_id="gh_gone")
        row = register_observation_sync(observation)

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(0, self.tracker.create_count)
        observation.refresh_from_db()
        self.assertEqual("", observation.issue_tracker_issue_id)
        self.assertTrue(observation.issue_tracker_issue_closed)

    # --- DELETED ----------------------------------------------------------------------

    def test_deleted_open_issue_gets_note_and_close_and_row_terminates(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        observation_id = observation.pk
        register_observation_deleted(observation)
        observation.delete()
        self.tracker.remote_issues = {"gh_1": self._open_issue()}

        row = Issue_Tracker_Status.objects.get(correlation_id=f"secobserveobservation{observation_id}")
        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.deleted_close_calls)
        self.assertEqual(0, Issue_Tracker_Status.objects.filter(pk=row.id).count())

    def test_deleted_without_remote_issue_terminates_row(self) -> None:
        observation = self._create_observation(issue_id="gh_gone")
        observation_id = observation.pk
        register_observation_deleted(observation)
        observation.delete()

        row = Issue_Tracker_Status.objects.get(correlation_id=f"secobserveobservation{observation_id}")
        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(0, self.tracker.deleted_close_calls)
        self.assertEqual(0, Issue_Tracker_Status.objects.filter(pk=row.id).count())

    # --- inactive product -------------------------------------------------------------

    def test_inactive_pending_open_without_issue_creates_nothing(self) -> None:
        observation = self._create_observation()
        row = self._create_status_row(observation)
        self.product.issue_tracker_active = False
        self.product.save()

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(0, self.tracker.create_count)
        self.assertEqual(0, Issue_Tracker_Status.objects.filter(pk=row.id).count())

    def test_inactive_with_existing_open_issue_closes_it(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = self._create_status_row(observation, issue_id="gh_1", desired_state=Desired_State.DESIRED_STATE_CLOSED)
        self.tracker.remote_issues = {"gh_1": self._open_issue()}
        self.product.issue_tracker_active = False
        self.product.save()

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.close_calls)
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_SUCCEEDED, row.sync_state)
        observation.refresh_from_db()
        self.assertTrue(observation.issue_tracker_issue_closed)

    def test_inactive_uncertain_row_is_reconciled_before_cancel(self) -> None:
        observation = self._create_observation()
        row = self._create_status_row(observation, create_uncertain=True)
        self.tracker.marker_found_id = "gh_orphan"
        self.tracker.remote_issues = {
            "gh_orphan": Issue(id="gh_orphan", title=TITLE, description="d", state=Issue_State.ISSUE_STATE_OPEN)
        }
        self.product.issue_tracker_active = False
        self.product.save()

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.find_calls)
        self.assertEqual(1, self.tracker.close_calls)
        self.assertEqual(0, self.tracker.create_count)
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_SUCCEEDED, row.sync_state)
        observation.refresh_from_db()
        self.assertEqual("gh_orphan", observation.issue_tracker_issue_id)
        self.assertTrue(observation.issue_tracker_issue_closed)

    def test_inactive_uncertain_row_confirmed_absent_is_canceled(self) -> None:
        observation = self._create_observation()
        row = self._create_status_row(observation, create_uncertain=True)
        self.product.issue_tracker_active = False
        self.product.save()

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.find_calls)
        self.assertEqual(0, self.tracker.create_count)
        self.assertEqual(0, Issue_Tracker_Status.objects.filter(pk=row.id).count())

    # --- marker reconciliation across desired states ----------------------------------

    def test_uncertain_open_marker_finds_closed_issue_and_reopens(self) -> None:
        observation = self._create_observation()
        row = self._create_status_row(observation, create_uncertain=True)
        self.tracker.marker_found_id = "gh_found"
        self.tracker.remote_issues = {
            "gh_found": Issue(id="gh_found", title="old", description="d", state=Issue_State.ISSUE_STATE_CLOSED)
        }

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.find_calls)
        self.assertEqual(1, self.tracker.update_calls)
        self.assertEqual(0, self.tracker.create_count)
        observation.refresh_from_db()
        self.assertEqual("gh_found", observation.issue_tracker_issue_id)
        row.refresh_from_db()
        self.assertFalse(row.create_uncertain)

    def test_uncertain_closed_marker_finds_open_issue_and_closes(self) -> None:
        observation = self._create_observation(status=Status.STATUS_RESOLVED)
        row = self._create_status_row(
            observation,
            desired_state=Desired_State.DESIRED_STATE_CLOSED,
            create_uncertain=True,
        )
        self.tracker.marker_found_id = "gh_found"
        self.tracker.remote_issues = {
            "gh_found": Issue(id="gh_found", title=TITLE, description="d", state=Issue_State.ISSUE_STATE_OPEN)
        }

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.find_calls)
        self.assertEqual(1, self.tracker.close_calls)
        self.assertEqual(0, self.tracker.create_count)

    def test_uncertain_deleted_marker_finds_issue_and_closes_with_note(self) -> None:
        observation = self._create_observation()
        row = self._create_status_row(
            observation,
            desired_state=Desired_State.DESIRED_STATE_DELETED,
            create_uncertain=True,
        )
        row.observation = None
        row.save()
        self.tracker.marker_found_id = "gh_found"
        self.tracker.remote_issues = {
            "gh_found": Issue(id="gh_found", title=TITLE, description="d", state=Issue_State.ISSUE_STATE_OPEN)
        }

        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.deleted_close_calls)
        self.assertEqual(0, Issue_Tracker_Status.objects.filter(pk=row.id).count())

    def test_marker_search_happens_before_create_after_uncertain_create(self) -> None:
        observation = self._create_observation()
        row = self._create_status_row(observation, create_uncertain=True, sync_state=Sync_State.SYNC_STATE_FAILED)
        row.attempts = 5
        row.save()

        from application.issue_tracker.services.issue_sync_registration import (
            reset_for_manual_retry,
        )

        self.assertTrue(reset_for_manual_retry(row))
        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.find_calls)
        self.assertEqual(1, self.tracker.create_count)

    # --- revisions during flight ------------------------------------------------------

    def test_rapid_revisions_during_slow_create_result_in_one_create_and_requeue(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        def bumps_during_create(_observation: Observation) -> str:
            Observation.objects.filter(pk=observation.pk).update(current_status=Status.STATUS_RESOLVED)
            observation.current_status = Status.STATUS_RESOLVED
            register_observation_sync(observation)
            register_observation_sync(observation)
            return "gh_1"

        self.tracker.create_side_effect = bumps_during_create
        self.tracker.remote_issues = {}

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertEqual(1, self.tracker.create_count)
        self.schedule_mock.assert_called_with(row.id)

        # Second run converges the newest revision (CLOSED), the created issue is closed.
        self.tracker.remote_issues = {"gh_1": self._open_issue(issue_id="gh_1")}
        self.tracker.create_side_effect = None
        issue_sync_executor.execute_sync(row.id)

        self.assertEqual(1, self.tracker.close_calls)
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_SUCCEEDED, row.sync_state)

    def test_deactivation_during_create_drives_created_issue_to_closed(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        def deactivate_during_create(_observation: Observation) -> str:
            close_all_for_deactivation(self.product)
            self.product.issue_tracker_active = False
            self.product.save()
            return "gh_new"

        self.tracker.create_side_effect = deactivate_during_create

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, row.desired_state)
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertEqual("gh_new", row.issue_id)

        self.tracker.remote_issues = {"gh_new": self._open_issue(issue_id="gh_new")}
        issue_sync_executor.execute_sync(row.id)
        self.assertEqual(1, self.tracker.close_calls)

    def test_deletion_during_first_create_is_converged_without_error(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        def delete_during_create(_observation: Observation) -> str:
            observation.delete()
            return "gh_new"

        self.tracker.create_side_effect = delete_during_create

        issue_sync_executor.execute_sync(row.id)

        remaining = Issue_Tracker_Status.objects.filter(product=self.product)
        self.assertEqual(1, remaining.count())
        deleted_row = remaining.first()
        self.assertEqual(Desired_State.DESIRED_STATE_DELETED, deleted_row.desired_state)
        self.assertEqual("gh_new", deleted_row.issue_id)

    # --- errors ------------------------------------------------------------------------

    def test_uncertain_create_sets_flag_and_backoff(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        self.tracker.create_side_effect = MagicMock(side_effect=IssueTrackerUncertainError("timeout"))

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertTrue(row.create_uncertain)
        self.assertIsNotNone(row.next_attempt_at)
        self.assertIn("timeout", row.last_error)

    def test_uncertain_update_sets_flag_and_retries(self) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)

        tracker = MagicTracker()
        tracker.remote_issues = {"gh_1": Issue(id="gh_1", title="old title", description="d")}
        tracker.update_issue = MagicMock(side_effect=IssueTrackerUncertainError("500"))
        self.factory_mock.return_value = tracker

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertTrue(row.create_uncertain)
        self.assertIsNotNone(row.next_attempt_at)

    def test_deterministic_error_marks_failed_and_notifies(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        self.tracker.create_side_effect = MagicMock(side_effect=IssueTrackerDeterministicError("403"))

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_FAILED, row.sync_state)
        self.assertIn("403", row.last_error)
        self.notify_mock.assert_called_once()

    def test_exhausted_attempts_mark_failed(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        row.attempts = issue_sync_executor.MAX_ATTEMPTS - 1
        row.save()
        self.tracker.create_side_effect = MagicMock(side_effect=IssueTrackerUncertainError("timeout"))

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_FAILED, row.sync_state)

    def test_newer_revision_during_uncertain_failure_requeues_immediately(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        def newer_revision_then_fail(_observation: Observation) -> str:
            register_observation_sync(observation)
            raise IssueTrackerUncertainError("timeout")

        self.tracker.create_side_effect = newer_revision_then_fail

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertTrue(row.create_uncertain)
        self.assertIsNotNone(row.next_attempt_at)
        self.assertTrue(row.next_attempt_at <= timezone.now() + timedelta(seconds=5))

    def test_unexpected_exception_becomes_terminal_failure(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        self.tracker.create_side_effect = MagicMock(side_effect=KeyError("boom"))

        issue_sync_executor.execute_sync(row.id)

        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_FAILED, row.sync_state)

    # --- combined restart scenario (R3 P6) --------------------------------------------

    def test_timeout_resolve_restart_dispatch_closes_found_issue_without_recreating(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        call_count = {"n": 0}

        def flaky_create(_observation: Observation) -> str:
            call_count["n"] += 1
            raise IssueTrackerUncertainError("ReadTimeout")

        self.tracker.create_side_effect = flaky_create
        issue_sync_executor.execute_sync(row.id)

        # Analyst resolves during the backoff.
        Observation.objects.filter(pk=observation.pk).update(current_status=Status.STATUS_RESOLVED)
        observation.current_status = Status.STATUS_RESOLVED
        register_observation_sync(observation)

        # Process dies mid-flight and is restarted.
        Issue_Tracker_Status.objects.filter(pk=row.pk).update(
            sync_state=Sync_State.SYNC_STATE_IN_PROGRESS,
            locked_at=timezone.now() - timedelta(minutes=30),
        )
        self.tracker.marker_found_id = "gh_found"
        self.tracker.remote_issues = {
            "gh_found": Issue(id="gh_found", title=TITLE, description="d", state=Issue_State.ISSUE_STATE_OPEN)
        }
        self.tracker.create_side_effect = MagicMock(side_effect=AssertionError("must not create again"))

        with patch("application.issue_tracker.services.issue_sync_dispatcher.trigger_observation_sync") as trigger_mock:
            # Immediate execution side effect: run execute_sync for the dispatched row.
            trigger_mock.side_effect = lambda row_id: issue_sync_executor.execute_sync(row_id)
            dispatch_pending()

        row.refresh_from_db()
        self.assertEqual(1, call_count["n"])
        self.assertEqual(1, self.tracker.find_calls)
        self.assertEqual(1, self.tracker.close_calls)
        self.assertEqual(Sync_State.SYNC_STATE_SUCCEEDED, row.sync_state)
        self.assertEqual("gh_found", row.issue_id)
