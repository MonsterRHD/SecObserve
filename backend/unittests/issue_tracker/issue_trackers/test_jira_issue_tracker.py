from unittest.mock import MagicMock, patch

from jira.exceptions import JIRAError

from application.core.types import Severity
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    Issue,
    Issue_State,
    IssueTrackerDeterministicError,
    IssueTrackerUncertainError,
)
from application.issue_tracker.issue_trackers.jira_issue_tracker import JiraIssueTracker
from unittests.base_test_case import BaseTestCase


class TestJiraIssueTracker(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.observation_1.pk = 1
        self.observation_1.current_severity = Severity.SEVERITY_CRITICAL
        self.observation_1.description = "description_1"
        self.observation_1.product.issue_tracker_project_id = "jira_project_1"
        self.observation_1.product.issue_tracker_base_url = "https://jira.com"
        self.observation_1.product.issue_tracker_labels = "label_1, label_2"
        self.observation_1.product.issue_tracker_issue_type = "Vulnerability"
        self.observation_1.product.issue_tracker_status_closed = "Done"

    def _tracker(self, jira_client):
        tracker = JiraIssueTracker.__new__(JiraIssueTracker)
        tracker.jira = jira_client
        return tracker

    def _jira_issue(self, key="J-1", status="Open", description="d", labels=None):
        jira_issue = MagicMock()
        jira_issue.key = key
        jira_issue.fields.summary = "title"
        jira_issue.fields.description = description
        jira_issue.fields.labels = labels or []
        jira_issue.fields.status = status
        return jira_issue

    def test_create_issue_returns_key_and_does_not_save_observation(self):
        jira_client = MagicMock()
        jira_client.create_issue.return_value = self._jira_issue("J-1", status="To Do")
        tracker = self._tracker(jira_client)

        with patch("application.issue_tracker.issue_trackers.jira_issue_tracker.get_base_url_frontend") as url_mock:
            url_mock.return_value = "https://secobserve.com/"
            issue_id = tracker.create_issue(self.observation_1)

        self.assertEqual("J-1", issue_id)
        self.assertEqual("", self.observation_1.issue_tracker_jira_initial_status)
        kwargs = jira_client.create_issue.call_args.kwargs
        self.assertEqual("jira_project_1", kwargs["project"])
        self.assertEqual("Vulnerability", kwargs["issuetype"])
        self.assertIn("{color:#888888}secobserveobservation1{color}", kwargs["description"])
        self.assertNotIn("**", kwargs["description"])

    def test_get_issue_state_mapping(self):
        jira_client = MagicMock()
        jira_client.issue.return_value = self._jira_issue(status="Done")
        tracker = self._tracker(jira_client)

        issue = tracker.get_issue(self.observation_1.product, "J-1")

        self.assertEqual(Issue_State.ISSUE_STATE_CLOSED, issue.state)
        self.assertEqual("Done", issue.jira_initial_status)

    def test_get_issue_404_returns_none(self):
        jira_client = MagicMock()
        jira_client.issue.side_effect = JIRAError(status_code=404, text="not found")
        tracker = self._tracker(jira_client)
        self.assertIsNone(tracker.get_issue(self.observation_1.product, "J-1"))

    def test_find_issue_verifies_description(self):
        jira_client = MagicMock()
        jira_client.search_issues.return_value = [
            self._jira_issue("J-9", description="noise"),
            self._jira_issue("J-42", description="x {color:#888888}secobserveobservation1{color}"),
        ]
        tracker = self._tracker(jira_client)

        self.assertEqual(
            "J-42", tracker.find_issue_id_by_correlation_id(self.observation_1.product, "secobserveobservation1")
        )
        jql = jira_client.search_issues.call_args.args[0]
        self.assertIn('project = "jira_project_1"', jql)

    def test_find_issue_rejects_fuzzy_result_without_marker(self):
        jira_client = MagicMock()
        jira_client.search_issues.return_value = [self._jira_issue("J-9", description="noise")]
        tracker = self._tracker(jira_client)
        self.assertIsNone(tracker.find_issue_id_by_correlation_id(self.observation_1.product, "secobserveobservation1"))

    def test_update_open_issue_does_not_transition(self):
        jira_client = MagicMock()
        jira_issue = self._jira_issue(status="In Progress")
        jira_client.issue.return_value = jira_issue
        tracker = self._tracker(jira_client)

        tracker.update_issue(self.observation_1, Issue(id="J-1", title="title"))

        jira_issue.update.assert_called_once()
        jira_client.transition_issue.assert_not_called()

    def test_update_closed_issue_reopens_with_stored_initial_status(self):
        self.observation_1.issue_tracker_jira_initial_status = "To Do"
        jira_client = MagicMock()
        jira_client.issue.return_value = self._jira_issue(status="Done")
        tracker = self._tracker(jira_client)

        tracker.update_issue(self.observation_1, Issue(id="J-1", title="title"))

        jira_client.transition_issue.assert_called_once()
        self.assertEqual("To Do", jira_client.transition_issue.call_args.args[1])

    def test_update_closed_issue_uses_transition_category_without_stored_status(self):
        jira_client = MagicMock()
        jira_client.issue.return_value = self._jira_issue(status="Done")
        jira_client.transitions.return_value = [
            {"name": "Close", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
            {"name": "Reopen", "to": {"name": "To Do", "statusCategory": {"key": "indeterminate"}}},
        ]
        tracker = self._tracker(jira_client)

        tracker.update_issue(self.observation_1, Issue(id="J-1", title="title"))

        self.assertEqual("To Do", jira_client.transition_issue.call_args.args[1])

    def test_close_issue_transitions_only_when_not_already_closed(self):
        jira_client = MagicMock()
        jira_client.issue.return_value = self._jira_issue(status="In Progress")
        tracker = self._tracker(jira_client)

        tracker.close_issue(self.observation_1, Issue(id="J-1", title="title"))

        jira_client.transition_issue.assert_called_once()
        self.assertEqual("Done", jira_client.transition_issue.call_args.args[1])

        jira_client.reset_mock()
        jira_client.issue.return_value = self._jira_issue(status="Done")
        tracker.close_issue(self.observation_1, Issue(id="J-1", title="title"))
        jira_client.transition_issue.assert_not_called()

    def test_close_for_deleted_observation(self):
        jira_client = MagicMock()
        jira_client.issue.return_value = self._jira_issue(status="In Progress", description="original")
        tracker = self._tracker(jira_client)

        tracker.close_issue_for_deleted_observation(
            self.observation_1.product, Issue(id="J-1", title="title", description="original")
        )

        jira_client.transition_issue.assert_called_once()
        updated_description = jira_client.issue.return_value.update.call_args.kwargs["description"]
        self.assertIn("*--- Observation has been deleted ---*", updated_description)

    def test_close_for_deleted_observation_is_idempotent(self):
        jira_client = MagicMock()
        jira_client.issue.return_value = self._jira_issue(
            status="In Progress", description="*--- Observation has been deleted ---*\nold"
        )
        tracker = self._tracker(jira_client)

        tracker.close_issue_for_deleted_observation(
            self.observation_1.product,
            Issue(id="J-1", title="title", description="*--- Observation has been deleted ---*\nold"),
        )

        updated_description = jira_client.issue.return_value.update.call_args.kwargs["description"]
        self.assertEqual("*--- Observation has been deleted ---*\nold", updated_description)

    def test_uncertain_jira_error(self):
        jira_client = MagicMock()
        jira_client.create_issue.side_effect = JIRAError(status_code=503, text="unavailable")
        tracker = self._tracker(jira_client)
        with self.assertRaises(IssueTrackerUncertainError):
            tracker.create_issue(self.observation_1)

    def test_deterministic_jira_error(self):
        jira_client = MagicMock()
        jira_client.create_issue.side_effect = JIRAError(status_code=403, text="forbidden")
        tracker = self._tracker(jira_client)
        with self.assertRaises(IssueTrackerDeterministicError):
            tracker.create_issue(self.observation_1)

    def test_correlation_marker_is_jira_colored(self):
        tracker = self._tracker(MagicMock())
        self.assertEqual(
            "{color:#888888}secobserveobservation1{color}",
            tracker.get_correlation_marker(self.observation_1),
        )
