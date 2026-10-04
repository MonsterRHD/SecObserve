from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from application.core.models import Branch, Observation, Product
from application.core.types import Severity, Status
from application.import_observations.models import Parser
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services import issue_sync_executor
from application.issue_tracker.services.issue_sync_registration import (
    _bump_status_row,
    _upsert_status_row,
    close_all_for_deactivation,
    compute_desired_state,
    register_observation_deleted,
    register_observation_sync,
    register_product_resync,
    reset_for_manual_retry,
)
from application.issue_tracker.types import Desired_State, Issue_Tracker, Sync_State
from unittests.base_test_case import BaseTestCase


class IssueTrackerRegistrationTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parser = Parser.objects.create(name="it_parser")
        self.product = Product.objects.create(
            name="it_product",
            issue_tracker_active=True,
            issue_tracker_type=Issue_Tracker.ISSUE_TRACKER_GITHUB,
            issue_tracker_project_id="org/repo",
            issue_tracker_status_closed="Closed",
        )
        self.branch = Branch.objects.create(name="main", product=self.product, is_default_branch=True)
        self.product.repository_default_branch = self.branch
        self.product.save()

    def _create_observation(
        self,
        *,
        status: str = Status.STATUS_OPEN,
        severity: str = Severity.SEVERITY_HIGH,
        branch: Branch = None,
        issue_id: str = "",
    ) -> Observation:
        return Observation.objects.create(
            title="observation",
            product=self.product,
            parser=self.parser,
            branch=branch or self.branch,
            parser_status=status,
            parser_severity=severity,
            import_last_seen=timezone.now(),
            issue_tracker_issue_id=issue_id,
        )

    # --- gating ---------------------------------------------------------------------

    def test_inactive_product_registers_nothing(self) -> None:
        self.product.issue_tracker_active = False
        self.product.save()
        observation = self._create_observation()
        self.assertIsNone(register_observation_sync(observation))
        self.assertEqual(0, Issue_Tracker_Status.objects.count())

    def test_non_default_branch_registers_nothing(self) -> None:
        other_branch = Branch.objects.create(name="dev", product=self.product)
        observation = self._create_observation(branch=other_branch)
        self.assertIsNone(register_observation_sync(observation))
        self.assertEqual(0, Issue_Tracker_Status.objects.count())

    # --- upsert / desired state ------------------------------------------------------

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_first_registration_creates_pending_open_row(self, schedule_mock) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)

        self.assertIsNotNone(row)
        self.assertEqual(Desired_State.DESIRED_STATE_OPEN, row.desired_state)
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertEqual(1, row.revision)
        self.assertEqual(f"secobserveobservation{observation.pk}", row.correlation_id)
        self.assertIsNotNone(row.next_attempt_at)
        schedule_mock.assert_called_once_with(row.id)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_resolved_observation_registers_closed(self, schedule_mock) -> None:
        observation = self._create_observation(status=Status.STATUS_RESOLVED)
        row = register_observation_sync(observation)
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, row.desired_state)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_below_minimum_severity_registers_closed(self, schedule_mock) -> None:
        self.product.issue_tracker_minimum_severity = Severity.SEVERITY_HIGH
        self.product.save()
        observation = self._create_observation(severity=Severity.SEVERITY_MEDIUM)
        row = register_observation_sync(observation)
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, row.desired_state)

    def test_compute_desired_state(self) -> None:
        open_observation = self._create_observation()
        self.assertEqual(Desired_State.DESIRED_STATE_OPEN, compute_desired_state(open_observation))

        self.product.issue_tracker_minimum_severity = Severity.SEVERITY_CRITICAL
        self.product.save()
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, compute_desired_state(open_observation))

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_second_registration_bumps_revision_without_losing_issue_id(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)
        row = register_observation_sync(observation)

        self.assertEqual(2, row.revision)
        self.assertEqual("gh_1", row.issue_id)
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)

    # --- lease ------------------------------------------------------------------------

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_bump_with_stale_row_view_keeps_fresh_lease(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_1")
        register_observation_sync(observation)

        stale_row = Issue_Tracker_Status.objects.get(observation=observation)
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, stale_row.sync_state)

        claimed = issue_sync_executor._claim_status_row(stale_row.id)
        self.assertIsNotNone(claimed)

        # The bumping request holds a pre-claim snapshot, as on MySQL REPEATABLE READ.
        _bump_status_row(stale_row, desired_state=Desired_State.DESIRED_STATE_OPEN, issue_id="gh_1")

        stale_row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_IN_PROGRESS, stale_row.sync_state)
        self.assertIsNotNone(stale_row.locked_at)
        self.assertEqual(2, stale_row.revision)
        self.assertIsNone(issue_sync_executor._claim_status_row(stale_row.id))

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_bump_on_stale_lease_resets_pending(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)
        claimed = issue_sync_executor._claim_status_row(row.id)
        claimed.locked_at = timezone.now() - timedelta(minutes=30)
        claimed.save(update_fields=["locked_at"])

        _bump_status_row(row, desired_state=Desired_State.DESIRED_STATE_CLOSED, issue_id="gh_1")
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertIsNone(row.locked_at)
        self.assertIsNotNone(issue_sync_executor._claim_status_row(row.id))

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_bump_preserves_create_uncertain_flag(self, schedule_mock) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        row.create_uncertain = True
        row.sync_state = Sync_State.SYNC_STATE_FAILED
        row.save()

        _bump_status_row(row, desired_state=Desired_State.DESIRED_STATE_CLOSED, issue_id="")
        row.refresh_from_db()
        self.assertTrue(row.create_uncertain)
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertEqual("", row.issue_id)

    # --- deletion ---------------------------------------------------------------------

    def test_delete_without_row_and_without_issue_id_registers_nothing(self) -> None:
        observation = self._create_observation()
        self.assertIsNone(register_observation_deleted(observation))
        self.assertEqual(0, Issue_Tracker_Status.objects.count())

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_delete_with_issue_id_creates_deleted_row(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_legacy")
        row = register_observation_deleted(observation)
        self.assertEqual(Desired_State.DESIRED_STATE_DELETED, row.desired_state)
        self.assertEqual("gh_legacy", row.issue_id)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_delete_bumps_existing_row_to_deleted_and_keeps_fresh_lease(self, schedule_mock) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        claimed = issue_sync_executor._claim_status_row(row.id)
        self.assertIsNotNone(claimed)

        register_observation_deleted(observation)

        row.refresh_from_db()
        self.assertEqual(Desired_State.DESIRED_STATE_DELETED, row.desired_state)
        self.assertEqual(Sync_State.SYNC_STATE_IN_PROGRESS, row.sync_state)
        self.assertIsNotNone(row.locked_at)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_deleted_row_survives_observation_deletion(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_1")
        observation_id = observation.pk
        row = register_observation_deleted(observation)
        observation.delete()

        row.refresh_from_db()
        self.assertIsNone(row.observation)
        self.assertEqual(self.product, row.product)
        self.assertEqual("gh_1", row.issue_id)
        self.assertEqual(f"secobserveobservation{observation_id}", row.correlation_id)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_uncertain_row_without_issue_id_survives_deletion(self, schedule_mock) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        row.create_uncertain = True
        row.save()

        register_observation_deleted(observation)
        observation.delete()

        row.refresh_from_db()
        self.assertIsNone(row.observation)
        self.assertEqual(Desired_State.DESIRED_STATE_DELETED, row.desired_state)

    def test_delete_inactive_product_registers_nothing(self) -> None:
        self.product.issue_tracker_active = False
        self.product.save()
        observation = self._create_observation(issue_id="gh_1")
        self.assertIsNone(register_observation_deleted(observation))

    # --- deactivation / resync --------------------------------------------------------

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_close_all_bumps_rows_to_closed_but_keeps_fresh_lease(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_sync(observation)
        claimed = issue_sync_executor._claim_status_row(row.id)
        self.assertIsNotNone(claimed)

        close_all_for_deactivation(self.product)

        row.refresh_from_db()
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, row.desired_state)
        self.assertEqual(Sync_State.SYNC_STATE_IN_PROGRESS, row.sync_state)
        self.assertIsNotNone(row.locked_at)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_close_all_creates_rows_for_legacy_observations_with_issue_id(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_legacy")
        close_all_for_deactivation(self.product)
        row = Issue_Tracker_Status.objects.get(observation=observation)
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, row.desired_state)
        self.assertEqual("gh_legacy", row.issue_id)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_close_all_does_not_create_rows_for_untracked_observations(self, schedule_mock) -> None:
        self._create_observation()
        close_all_for_deactivation(self.product)
        self.assertEqual(0, Issue_Tracker_Status.objects.count())

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_deleted_rows_are_excluded_from_deactivation(self, schedule_mock) -> None:
        observation = self._create_observation(issue_id="gh_1")
        row = register_observation_deleted(observation)
        observation.delete()
        close_all_for_deactivation(self.product)
        row.refresh_from_db()
        self.assertEqual(Desired_State.DESIRED_STATE_DELETED, row.desired_state)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_product_resync_registers_default_branch_observations(self, schedule_mock) -> None:
        observation = self._create_observation()
        count = register_product_resync(self.product)
        self.assertEqual(1, count)
        row = Issue_Tracker_Status.objects.get(observation=observation)
        self.assertEqual(Desired_State.DESIRED_STATE_OPEN, row.desired_state)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_product_resync_closes_issues_of_observations_that_left_default_branch(self, schedule_mock) -> None:
        new_default = Branch.objects.create(name="release", product=self.product)
        observation = self._create_observation(branch=self.branch, issue_id="gh_1")
        self.product.repository_default_branch = new_default
        self.product.save()

        register_product_resync(self.product)

        row = Issue_Tracker_Status.objects.get(observation=observation)
        self.assertEqual(Desired_State.DESIRED_STATE_CLOSED, row.desired_state)

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_product_resync_ignores_irrelevant_non_default_observations(self, schedule_mock) -> None:
        other_branch = Branch.objects.create(name="dev", product=self.product)
        self._create_observation(branch=other_branch)
        register_product_resync(self.product)
        self.assertEqual(0, Issue_Tracker_Status.objects.count())

    def test_product_resync_inactive_is_noop(self) -> None:
        self.product.issue_tracker_active = False
        self.product.save()
        self._create_observation()
        self.assertEqual(0, register_product_resync(self.product))

    # --- manual retry -----------------------------------------------------------------

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_manual_retry_resets_failed_row_but_keeps_uncertain_flag(self, schedule_mock) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        row.sync_state = Sync_State.SYNC_STATE_FAILED
        row.attempts = 5
        row.create_uncertain = True
        row.save()

        self.assertTrue(reset_for_manual_retry(row))
        row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, row.sync_state)
        self.assertEqual(0, row.attempts)
        self.assertTrue(row.create_uncertain)

    def test_manual_retry_rejects_succeeded_and_fresh_in_progress(self) -> None:
        observation = self._create_observation()
        row = register_observation_sync(observation)
        row.sync_state = Sync_State.SYNC_STATE_SUCCEEDED
        row.save()
        self.assertFalse(reset_for_manual_retry(row))

        row.sync_state = Sync_State.SYNC_STATE_PENDING
        row.locked_at = None
        row.save()
        claimed = issue_sync_executor._claim_status_row(row.id)
        self.assertIsNotNone(claimed)
        self.assertFalse(reset_for_manual_retry(claimed))

    # --- concurrent first insert ------------------------------------------------------

    @patch("application.issue_tracker.services.issue_sync_registration.schedule_observation_sync")
    def test_upsert_fallback_after_concurrent_first_insert(self, schedule_mock) -> None:
        observation = self._create_observation()
        # Another transaction committed the first row while this transaction still had a
        # stale view. Locking re-read finds the row and bumps it instead of failing.
        Issue_Tracker_Status.objects.create(
            product=self.product,
            observation=observation,
            correlation_id=f"secobserveobservation{observation.pk}",
            issue_id="",
            desired_state=Desired_State.DESIRED_STATE_OPEN,
            revision=1,
            sync_state=Sync_State.SYNC_STATE_PENDING,
        )

        row = _upsert_status_row(
            product=self.product,
            observation=observation,
            issue_id="",
            desired_state=Desired_State.DESIRED_STATE_OPEN,
            status_row=None,
        )

        self.assertEqual(2, row.revision)
