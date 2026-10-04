from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from application.core.models import Branch, Observation, Product
from application.core.types import Severity, Status
from application.import_observations.models import Parser
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services.issue_sync_dispatcher import (
    dispatch_pending,
    recover_after_restart,
)
from application.issue_tracker.types import Desired_State, Issue_Tracker, Sync_State
from unittests.base_test_case import BaseTestCase


class IssueSyncDispatcherTestCase(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parser = Parser.objects.create(name="dispatch_parser")
        self.product = Product.objects.create(
            name="dispatch_product",
            issue_tracker_active=True,
            issue_tracker_type=Issue_Tracker.ISSUE_TRACKER_GITHUB,
            issue_tracker_project_id="org/repo",
        )
        self.branch = Branch.objects.create(name="main", product=self.product, is_default_branch=True)
        self.product.repository_default_branch = self.branch
        self.product.save()

    def _row(
        self,
        *,
        sync_state: str,
        next_attempt_at=None,
        locked_at=None,
        desired_state: str = Desired_State.DESIRED_STATE_OPEN,
    ) -> Issue_Tracker_Status:
        observation = Observation.objects.create(
            title=f"obs_{Issue_Tracker_Status.objects.count()}",
            product=self.product,
            parser=self.parser,
            branch=self.branch,
            parser_status=Status.STATUS_OPEN,
            parser_severity=Severity.SEVERITY_HIGH,
            import_last_seen=timezone.now(),
        )
        return Issue_Tracker_Status.objects.create(
            product=self.product,
            observation=observation,
            correlation_id=f"secobserveobservation{observation.pk}",
            desired_state=desired_state,
            revision=1,
            sync_state=sync_state,
            next_attempt_at=next_attempt_at,
            locked_at=locked_at,
        )

    def test_recover_requeues_stale_in_progress_only(self) -> None:
        stale = self._row(sync_state=Sync_State.SYNC_STATE_IN_PROGRESS, locked_at=timezone.now() - timedelta(hours=1))
        fresh = self._row(sync_state=Sync_State.SYNC_STATE_IN_PROGRESS, locked_at=timezone.now())

        recovered = recover_after_restart()

        self.assertEqual(1, recovered)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, stale.sync_state)
        self.assertEqual(Sync_State.SYNC_STATE_IN_PROGRESS, fresh.sync_state)

    @patch("application.issue_tracker.services.issue_sync_dispatcher.trigger_observation_sync")
    def test_dispatch_picks_only_due_pending_rows(self, trigger_mock) -> None:
        due = self._row(sync_state=Sync_State.SYNC_STATE_PENDING, next_attempt_at=timezone.now() - timedelta(minutes=1))
        waiting = self._row(
            sync_state=Sync_State.SYNC_STATE_PENDING,
            next_attempt_at=timezone.now() + timedelta(hours=2),
        )
        self._row(sync_state=Sync_State.SYNC_STATE_SUCCEEDED)
        self._row(sync_state=Sync_State.SYNC_STATE_FAILED)

        dispatch_pending()

        triggered_ids = [call.args[0] for call in trigger_mock.call_args_list]
        self.assertEqual([due.id], triggered_ids)
        self.assertNotIn(waiting.id, triggered_ids)

    @patch("application.issue_tracker.services.issue_sync_dispatcher.trigger_observation_sync")
    def test_dispatch_recovers_stale_in_progress_in_same_run(self, trigger_mock) -> None:
        stale = self._row(
            sync_state=Sync_State.SYNC_STATE_IN_PROGRESS,
            locked_at=timezone.now() - timedelta(minutes=20),
        )

        dispatch_pending()

        triggered_ids = [call.args[0] for call in trigger_mock.call_args_list]
        self.assertEqual([stale.id], triggered_ids)
