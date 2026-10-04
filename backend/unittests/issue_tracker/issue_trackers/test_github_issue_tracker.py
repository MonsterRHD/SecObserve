import json
from unittest.mock import patch

from application.core.types import Severity, Status
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    Issue,
    Issue_State,
    IssueTrackerDeterministicError,
    IssueTrackerUncertainError,
)
from application.issue_tracker.issue_trackers.github_issue_tracker import (
    GitHubIssueTracker,
)
from unittests.base_test_case import BaseTestCase

REQUEST_MODULE = "application.issue_tracker.issue_trackers.github_issue_tracker.request_issue_tracker"


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, reason: str = "OK"):
        self._payload = payload
        self.status_code = status_code
        self.reason = reason

    def json(self):
        return self._payload


class TestGitHubIssueTracker(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.observation_1.pk = 1
        self.observation_1.current_severity = Severity.SEVERITY_CRITICAL
        self.observation_1.description = "description_1"
        self.observation_1.product.issue_tracker_project_id = "gh_project_1"
        self.observation_1.product.issue_tracker_labels = "label_1, label_2"
        self.observation_1.product.issue_tracker_api_key = "api_key_1"

    @patch(REQUEST_MODULE)
    def test_create_issue(self, request_mock):
        request_mock.return_value = FakeResponse({"number": "gh_1"})

        issue_id = GitHubIssueTracker().create_issue(self.observation_1)

        self.assertEqual("gh_1", issue_id)
        _, kwargs = request_mock.call_args
        self.assertEqual("POST", request_mock.call_args.args[0])
        self.assertEqual("https://api.github.com/repos/gh_project_1/issues", kwargs["url"])
        data = json.loads(kwargs["data"])
        self.assertEqual('Critical vulnerability: "observation_1"', data["title"])
        self.assertIn("<!-- secobserveobservation1 -->", data["body"])
        self.assertEqual(["label_1", "label_2"], data["labels"])

    @patch(REQUEST_MODULE)
    def test_get_issue_not_found(self, request_mock):
        request_mock.return_value = FakeResponse({}, status_code=404, reason="Not Found")
        self.assertIsNone(GitHubIssueTracker().get_issue(self.observation_1.product, "gh_1"))

    @patch(REQUEST_MODULE)
    def test_get_issue_success(self, request_mock):
        request_mock.return_value = FakeResponse(
            {
                "number": "gh_1",
                "title": "title_1",
                "body": "description_1",
                "state": "closed",
                "labels": [{"name": "label_1"}, {"name": "label_2"}],
            }
        )

        issue = GitHubIssueTracker().get_issue(self.observation_1.product, "gh_1")

        self.assertEqual(
            Issue(
                id="gh_1",
                title="title_1",
                description="description_1",
                labels="label_1,label_2",
                state=Issue_State.ISSUE_STATE_CLOSED,
            ),
            issue,
        )

    @patch(REQUEST_MODULE)
    def test_find_issue_id_by_correlation_id_verifies_body(self, request_mock):
        request_mock.return_value = FakeResponse(
            {
                "items": [
                    {"number": "gh_9", "body": "unrelated"},
                    {"number": "gh_42", "body": "<!-- secobserveobservation1 -->"},
                ]
            }
        )

        issue_id = GitHubIssueTracker().find_issue_id_by_correlation_id(
            self.observation_1.product, "secobserveobservation1"
        )

        self.assertEqual("gh_42", issue_id)
        self.assertIn("secobserveobservation1", kwargs_q(request_mock))

    @patch(REQUEST_MODULE)
    def test_find_issue_id_without_match(self, request_mock):
        request_mock.return_value = FakeResponse({"items": [{"number": "9", "body": "unrelated"}]})
        self.assertIsNone(
            GitHubIssueTracker().find_issue_id_by_correlation_id(self.observation_1.product, "secobserveobservation1")
        )

    @patch(REQUEST_MODULE)
    def test_update_issue_reopens_with_issue_id_from_issue_object(self, request_mock):
        request_mock.return_value = FakeResponse({"number": "gh_1"})
        self.observation_1.issue_tracker_issue_id = ""  # adopted rows may not have the mirror yet
        issue = Issue(id="gh_1", title="old", description="d", state=Issue_State.ISSUE_STATE_CLOSED)

        GitHubIssueTracker().update_issue(self.observation_1, issue)

        _, kwargs = request_mock.call_args
        self.assertTrue(kwargs["url"].endswith("/issues/gh_1"))
        self.assertEqual("open", json.loads(kwargs["data"])["state"])

    @patch(REQUEST_MODULE)
    def test_close_issue(self, request_mock):
        request_mock.return_value = FakeResponse({"number": "gh_1"})
        self.observation_1.current_status = Status.STATUS_RESOLVED
        issue = Issue(id="gh_1", title="t", description="d", labels="label_9")
        self.observation_1.product.issue_tracker_labels = "label_2,label_3"

        GitHubIssueTracker().close_issue(self.observation_1, issue)

        _, kwargs = request_mock.call_args
        data = json.loads(kwargs["data"])
        self.assertEqual("closed", data["state"])
        self.assertIn("**Observation status:** Resolved", data["body"])
        self.assertEqual(["label_2", "label_3", "label_9"], data["labels"])

    @patch(REQUEST_MODULE)
    def test_close_for_deleted_observation(self, request_mock):
        request_mock.return_value = FakeResponse({})
        issue = Issue(id="gh_1", title="t", description="description_1")

        GitHubIssueTracker().close_issue_for_deleted_observation(self.observation_1.product, issue)

        _, kwargs = request_mock.call_args
        data = json.loads(kwargs["data"])
        self.assertEqual("closed", data["state"])
        self.assertTrue(data["body"].startswith("**--- Observation has been deleted ---**"))

    @patch(REQUEST_MODULE)
    def test_close_for_deleted_observation_is_idempotent(self, request_mock):
        request_mock.return_value = FakeResponse({})
        issue = Issue(
            id="gh_1",
            title="t",
            description="**--- Observation has been deleted ---**\nold",
        )

        GitHubIssueTracker().close_issue_for_deleted_observation(self.observation_1.product, issue)

        _, kwargs = request_mock.call_args
        data = json.loads(kwargs["data"])
        self.assertEqual("closed", data["state"])
        self.assertNotIn("body", data)

    @patch(REQUEST_MODULE)
    def test_uncertain_status_raises_uncertain_error(self, request_mock):
        request_mock.side_effect = IssueTrackerUncertainError("503")
        with self.assertRaises(IssueTrackerUncertainError):
            GitHubIssueTracker().create_issue(self.observation_1)

    @patch(REQUEST_MODULE)
    def test_deterministic_status_raises_deterministic_error(self, request_mock):
        request_mock.side_effect = IssueTrackerDeterministicError("403")
        with self.assertRaises(IssueTrackerDeterministicError):
            GitHubIssueTracker().get_issue(self.observation_1.product, "gh_1")

    def test_get_frontend_issue_url(self):
        url = GitHubIssueTracker().get_frontend_issue_url(self.observation_1.product, "gh_1")
        self.assertEqual("https://github.com/gh_project_1/issues/gh_1", url)


def kwargs_q(request_mock) -> str:
    return request_mock.call_args.kwargs["params"]["q"]
