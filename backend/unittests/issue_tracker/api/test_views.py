from unittest.mock import patch

from django.utils import timezone
from rest_framework.status import (
    HTTP_202_ACCEPTED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)
from rest_framework.test import APIClient

from application.access_control.models import User
from application.authorization.services.roles_permissions import Roles
from application.core.models import Branch, Observation, Product, Product_Member
from application.core.types import Severity, Status
from application.import_observations.models import Parser
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.types import Desired_State, Issue_Tracker, Sync_State
from unittests.base_test_case import BaseTestCase

AUTHENTICATION_PATCH = (
    "application.access_control.services.api_token_authentication.APITokenAuthentication.authenticate"
)


class TestIssueTrackerStatusAPI(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api_client = APIClient()
        self.user = User.objects.create(username="it_owner@example.com")
        self.parser = Parser.objects.create(name="it_api_parser")
        self.product = Product.objects.create(
            name="it_api_product",
            issue_tracker_active=True,
            issue_tracker_type=Issue_Tracker.ISSUE_TRACKER_GITHUB,
            issue_tracker_project_id="org/repo",
        )
        self.branch = Branch.objects.create(name="main", product=self.product, is_default_branch=True)
        self.product.repository_default_branch = self.branch
        self.product.save()
        Product_Member.objects.create(product=self.product, user=self.user, role=Roles.Owner)

        self.observation = Observation.objects.create(
            title="obs",
            product=self.product,
            parser=self.parser,
            branch=self.branch,
            parser_status=Status.STATUS_OPEN,
            parser_severity=Severity.SEVERITY_HIGH,
            import_last_seen=timezone.now(),
        )
        self.status_row = Issue_Tracker_Status.objects.create(
            product=self.product,
            observation=self.observation,
            correlation_id="secobserveobservation1",
            desired_state=Desired_State.DESIRED_STATE_OPEN,
            revision=1,
            sync_state=Sync_State.SYNC_STATE_FAILED,
            last_error="503",
        )

    @patch(AUTHENTICATION_PATCH)
    def test_list_contains_status_row_for_product_member(self, mock_authentication):
        mock_authentication.return_value = self.user, None
        response = self.api_client.get(
            "/api/issue_tracker_statuses/", {"product": self.product.pk, "sync_state": "FAILED"}
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.data["count"])
        result = response.data["results"][0]
        self.assertEqual("FAILED", result["sync_state"])
        self.assertEqual("503", result["last_error"])
        self.assertEqual("it_api_product", result["product_name"])

    @patch(AUTHENTICATION_PATCH)
    def test_list_hides_rows_of_other_products(self, mock_authentication):
        other_user = User.objects.create(username="it_other@example.com")
        mock_authentication.return_value = other_user, None
        response = self.api_client.get("/api/issue_tracker_statuses/")
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, response.data["count"])

    @patch(AUTHENTICATION_PATCH)
    def test_retry_failed_row(self, mock_authentication):
        mock_authentication.return_value = self.user, None
        response = self.api_client.post(f"/api/issue_tracker_statuses/{self.status_row.pk}/retry/")
        self.assertEqual(HTTP_202_ACCEPTED, response.status_code)
        self.status_row.refresh_from_db()
        self.assertEqual(Sync_State.SYNC_STATE_PENDING, self.status_row.sync_state)
        self.assertEqual(0, self.status_row.attempts)

    @patch(AUTHENTICATION_PATCH)
    def test_retry_succeeded_row_conflicts(self, mock_authentication):
        mock_authentication.return_value = self.user, None
        self.status_row.sync_state = Sync_State.SYNC_STATE_SUCCEEDED
        self.status_row.save()
        response = self.api_client.post(f"/api/issue_tracker_statuses/{self.status_row.pk}/retry/")
        self.assertEqual(HTTP_409_CONFLICT, response.status_code)

    @patch(AUTHENTICATION_PATCH)
    def test_retry_forbidden_for_non_member(self, mock_authentication):
        other_user = User.objects.create(username="it_external@example.com", is_external=True)
        mock_authentication.return_value = other_user, None
        response = self.api_client.post(f"/api/issue_tracker_statuses/{self.status_row.pk}/retry/")
        self.assertEqual(HTTP_404_NOT_FOUND, response.status_code)
