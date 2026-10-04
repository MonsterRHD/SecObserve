from unittest.mock import patch

from application.core.types import Status
from application.issue_tracker.issue_trackers.github_issue_tracker import (
    GitHubIssueTracker,
)
from application.issue_tracker.issue_trackers.gitlab_issue_tracker import (
    GitLabIssueTracker,
)
from application.issue_tracker.services.issue_tracker import (
    issue_tracker_factory,
    push_observation_to_issue_tracker,
    push_observations_to_issue_tracker,
)
from application.issue_tracker.types import Issue_Tracker
from unittests.base_test_case import BaseTestCase


class TestIssueTrackerFacade(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.observation_1.product.issue_tracker_active = False
        self.observation_1.product.repository_default_branch = None

    @patch("application.issue_tracker.services.issue_tracker.register_observation_sync")
    def test_push_observations_to_issue_tracker_not_active(self, register_mock):
        push_observations_to_issue_tracker(self.product_1, (self.observation_1,))
        register_mock.assert_not_called()

    @patch("application.issue_tracker.services.issue_tracker.register_observation_sync")
    def test_push_observations_to_issue_tracker(self, register_mock):
        self.observation_1.product.issue_tracker_active = True
        self.observation_1.product.repository_default_branch = self.branch_1
        push_observations_to_issue_tracker(self.observation_1.product, (self.observation_1,))
        register_mock.assert_called_once_with(self.observation_1)

    @patch("application.issue_tracker.services.issue_tracker.register_observation_sync")
    def test_push_observation_to_issue_tracker_not_active(self, register_mock):
        push_observation_to_issue_tracker(self.observation_1, None)
        register_mock.assert_not_called()

    @patch("application.issue_tracker.services.issue_tracker.register_observation_sync")
    def test_push_observation_to_issue_tracker_not_default_branch(self, register_mock):
        self.observation_1.product.issue_tracker_active = True
        self.observation_1.product.repository_default_branch = self.branch_1
        self.observation_1.branch = self.branch_2
        push_observation_to_issue_tracker(self.observation_1, None)
        register_mock.assert_not_called()

    @patch("application.issue_tracker.services.issue_tracker.register_observation_sync")
    def test_push_observation_to_issue_tracker_registers_desired_state(self, register_mock):
        self.observation_1.product.issue_tracker_active = True
        self.observation_1.product.repository_default_branch = self.branch_1
        self.observation_1.branch = self.branch_1
        self.observation_1.current_status = Status.STATUS_OPEN
        push_observation_to_issue_tracker(self.observation_1, None)
        register_mock.assert_called_once_with(self.observation_1)

    def test_issue_tracker_factory_github(self):
        product = self.product_1
        product.issue_tracker_type = Issue_Tracker.ISSUE_TRACKER_GITHUB
        self.assertIsInstance(issue_tracker_factory(product), GitHubIssueTracker)

    def test_issue_tracker_factory_gitlab(self):
        product = self.product_1
        product.issue_tracker_type = Issue_Tracker.ISSUE_TRACKER_GITLAB
        self.assertIsInstance(issue_tracker_factory(product), GitLabIssueTracker)

    def test_issue_tracker_factory_jira_without_communication(self):
        product = self.product_1
        product.issue_tracker_type = Issue_Tracker.ISSUE_TRACKER_JIRA
        issue_tracker = issue_tracker_factory(product, with_communication=False)
        self.assertEqual("jira", issue_tracker.__class__.__name__.replace("IssueTracker", "").lower())

    def test_issue_tracker_factory_unknown(self):
        product = self.product_1
        product.issue_tracker_type = "invalid"
        with self.assertRaises(ValueError):
            issue_tracker_factory(product)
