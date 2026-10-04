from unittest.mock import patch

import requests

from application.issue_tracker.issue_trackers.base_issue_tracker import (
    IssueTrackerDeterministicError,
    IssueTrackerUncertainError,
)
from application.issue_tracker.issue_trackers.request_helper import (
    request_issue_tracker,
)
from unittests.base_test_case import BaseTestCase


class ResponseStub:
    def __init__(self, status_code: int, reason: str = "reason"):
        self.status_code = status_code
        self.reason = reason
        self.url = "https://example.com"


class TestRequestHelper(BaseTestCase):
    @patch("application.issue_tracker.issue_trackers.request_helper.requests.request")
    def test_timeout_is_uncertain(self, request_mock):
        request_mock.side_effect = requests.exceptions.Timeout("timed out")
        with self.assertRaises(IssueTrackerUncertainError):
            request_issue_tracker("GET", "https://example.com")

    @patch("application.issue_tracker.issue_trackers.request_helper.requests.request")
    def test_connection_error_is_uncertain(self, request_mock):
        request_mock.side_effect = requests.exceptions.ConnectionError("reset")
        with self.assertRaises(IssueTrackerUncertainError):
            request_issue_tracker("GET", "https://example.com")

    @patch("application.issue_tracker.issue_trackers.request_helper.requests.request")
    def test_retryable_statuses_are_uncertain(self, request_mock):
        for status_code in (408, 409, 425, 429, 500, 502, 503, 504):
            request_mock.return_value = ResponseStub(status_code)
            with self.assertRaises(IssueTrackerUncertainError):
                request_issue_tracker("GET", "https://example.com")

    @patch("application.issue_tracker.issue_trackers.request_helper.requests.request")
    def test_client_errors_are_deterministic(self, request_mock):
        for status_code in (400, 401, 403, 422):
            request_mock.return_value = ResponseStub(status_code)
            with self.assertRaises(IssueTrackerDeterministicError):
                request_issue_tracker("GET", "https://example.com")

    @patch("application.issue_tracker.issue_trackers.request_helper.requests.request")
    def test_404_and_success_are_returned(self, request_mock):
        request_mock.return_value = ResponseStub(404)
        self.assertEqual(404, request_issue_tracker("GET", "https://example.com").status_code)

        request_mock.return_value = ResponseStub(200)
        self.assertEqual(200, request_issue_tracker("GET", "https://example.com").status_code)
