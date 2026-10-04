from unittest.mock import patch

from application.core.types import Severity, Status
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    Issue,
    Issue_State,
)
from application.issue_tracker.issue_trackers.gitlab_issue_tracker import (
    GitLabIssueTracker,
)
from unittests.base_test_case import BaseTestCase

REQUEST_MODULE = "application.issue_tracker.issue_trackers.gitlab_issue_tracker.request_issue_tracker"


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class TestGitLabIssueTracker(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.observation_1.pk = 1
        self.observation_1.current_severity = Severity.SEVERITY_HIGH
        self.observation_1.description = "description_1"
        self.observation_1.product.issue_tracker_project_id = "gl_group/gl_project"
        self.observation_1.product.issue_tracker_base_url = "https://gitlab.com"
        self.observation_1.product.issue_tracker_labels = "label_1"
        self.observation_1.product.issue_tracker_api_key = "api_key_1"

    @patch(REQUEST_MODULE)
    def test_create_issue(self, request_mock):
        request_mock.return_value = FakeResponse({"iid": "gl_1"})

        issue_id = GitLabIssueTracker().create_issue(self.observation_1)

        self.assertEqual("gl_1", issue_id)
        args, kwargs = request_mock.call_args
        self.assertEqual("POST", args[0])
        self.assertEqual(
            "https://gitlab.com/api/v4/projects/gl_group%2Fgl_project/issues",
            kwargs["url"],
        )
        self.assertIn("<!-- secobserveobservation1 -->", kwargs["data"]["description"])
        self.assertTrue(kwargs["data"]["confidential"])
        self.assertEqual("label_1", kwargs["data"]["labels"])

    @patch(REQUEST_MODULE)
    def test_get_issue_not_found(self, request_mock):
        request_mock.return_value = FakeResponse({}, status_code=404)
        self.assertIsNone(GitLabIssueTracker().get_issue(self.observation_1.product, "gl_1"))

    @patch(REQUEST_MODULE)
    def test_get_issue_success(self, request_mock):
        request_mock.return_value = FakeResponse(
            {
                "iid": "gl_1",
                "title": "title_1",
                "description": "description_1",
                "state": "closed",
                "labels": ["label_1", "label_2"],
            }
        )

        issue = GitLabIssueTracker().get_issue(self.observation_1.product, "gl_1")

        self.assertEqual(
            Issue(
                id="gl_1",
                title="title_1",
                description="description_1",
                labels="label_1,label_2",
                state=Issue_State.ISSUE_STATE_CLOSED,
            ),
            issue,
        )

    @patch(REQUEST_MODULE)
    def test_find_issue_id_verifies_description(self, request_mock):
        request_mock.return_value = FakeResponse(
            [
                {"iid": "7", "description": "noise"},
                {"iid": "42", "description": "<!-- secobserveobservation1 -->"},
            ]
        )

        issue_id = GitLabIssueTracker().find_issue_id_by_correlation_id(
            self.observation_1.product, "secobserveobservation1"
        )

        self.assertEqual("42", issue_id)
        _, kwargs = request_mock.call_args
        self.assertEqual("secobserveobservation1", kwargs["data"]["search"])
        self.assertEqual("description", kwargs["data"]["in"])
        self.assertEqual("all", kwargs["data"]["state"])

    @patch(REQUEST_MODULE)
    def test_update_issue_reopens_and_adds_labels(self, request_mock):
        issue = Issue(id="gl_1", title="old", description="d")
        GitLabIssueTracker().update_issue(self.observation_1, issue)

        args, kwargs = request_mock.call_args
        self.assertEqual("PUT", args[0])
        self.assertTrue(kwargs["url"].endswith("/issues/gl_1"))
        self.assertEqual("reopen", kwargs["data"]["state_event"])
        self.assertEqual("label_1", kwargs["data"]["add_labels"])

    @patch(REQUEST_MODULE)
    def test_close_issue(self, request_mock):
        self.observation_1.current_status = Status.STATUS_RESOLVED
        issue = Issue(id="gl_1", title="t", description="d")

        GitLabIssueTracker().close_issue(self.observation_1, issue)

        _, kwargs = request_mock.call_args
        self.assertEqual("close", kwargs["data"]["state_event"])
        self.assertIn("**Observation status:** Resolved", kwargs["data"]["description"])

    @patch(REQUEST_MODULE)
    def test_close_for_deleted_observation(self, request_mock):
        issue = Issue(id="gl_1", title="t", description="description_1")
        GitLabIssueTracker().close_issue_for_deleted_observation(self.observation_1.product, issue)

        _, kwargs = request_mock.call_args
        self.assertEqual("close", kwargs["data"]["state_event"])
        self.assertTrue(kwargs["data"]["description"].startswith("**--- Observation has been deleted ---**"))

    @patch(REQUEST_MODULE)
    def test_close_for_deleted_observation_is_idempotent(self, request_mock):
        issue = Issue(
            id="gl_1",
            title="t",
            description="**--- Observation has been deleted ---**\nold",
        )
        GitLabIssueTracker().close_issue_for_deleted_observation(self.observation_1.product, issue)

        _, kwargs = request_mock.call_args
        self.assertNotIn("description", kwargs["data"])
        self.assertEqual("close", kwargs["data"]["state_event"])
