import urllib.parse
from typing import Optional

from application.core.models import Observation, Product
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    BaseIssueTracker,
    Issue,
    Issue_State,
)
from application.issue_tracker.issue_trackers.request_helper import (
    request_issue_tracker,
)


class GitLabIssueTracker(BaseIssueTracker):
    def create_issue(self, observation: Observation) -> str:
        data = {
            "title": self._get_title(observation),
            "description": self._get_description(observation),
            "confidential": True,
        }
        if observation.product.issue_tracker_labels:
            data["labels"] = observation.product.issue_tracker_labels
        response = request_issue_tracker(
            "POST",
            url=self._get_issue_tracker_base_url(observation.product),
            headers=self._get_headers(observation.product),
            data=data,
        )
        return str(response.json().get("iid"))

    def get_issue(self, product: Product, issue_id: str) -> Optional[Issue]:
        response = request_issue_tracker(
            "GET",
            url=f"{self._get_issue_tracker_base_url(product)}/{issue_id}",
            headers=self._get_headers(product),
        )
        if response.status_code == 404:
            return None

        payload = response.json()
        return Issue(
            id=str(payload.get("iid")),
            title=payload.get("title"),
            description=payload.get("description"),
            labels=",".join(payload.get("labels") or []),
            state=self._get_state(payload.get("state")),
        )

    def update_issue(self, observation: Observation, issue: Issue) -> None:
        data = {
            "title": self._get_title(observation),
            "description": self._get_description(observation),
            "confidential": True,
            "state_event": "reopen",
        }
        if observation.product.issue_tracker_labels:
            data["add_labels"] = observation.product.issue_tracker_labels
        request_issue_tracker(
            "PUT",
            url=f"{self._get_issue_tracker_base_url(observation.product)}/{issue.id}",
            headers=self._get_headers(observation.product),
            data=data,
        )

    def close_issue(self, observation: Observation, issue: Issue) -> None:
        description = self._get_description(observation)
        description += f"\n\n**Observation status:** {observation.current_status}"

        data = {
            "title": self._get_title(observation),
            "description": description,
            "confidential": True,
            "state_event": "close",
        }
        if observation.product.issue_tracker_labels:
            data["add_labels"] = observation.product.issue_tracker_labels
        request_issue_tracker(
            "PUT",
            url=f"{self._get_issue_tracker_base_url(observation.product)}/{issue.id}",
            headers=self._get_headers(observation.product),
            data=data,
        )

    def close_issue_for_deleted_observation(self, product: Product, issue: Issue) -> None:
        # If the deletion note is already present, only close the issue without adding
        # the note a second time.
        if self._already_has_deletion_note(issue):
            data = {"confidential": True, "state_event": "close"}
        else:
            data = {
                "description": self._get_description_for_deleted_observation(issue.description),
                "confidential": True,
                "state_event": "close",
            }
        if product.issue_tracker_labels:
            data["add_labels"] = product.issue_tracker_labels
        request_issue_tracker(
            "PUT",
            url=f"{self._get_issue_tracker_base_url(product)}/{issue.id}",
            headers=self._get_headers(product),
            data=data,
        )

    def find_issue_id_by_correlation_id(self, product: Product, correlation_id: str) -> Optional[str]:
        response = request_issue_tracker(
            "GET",
            url=self._get_issue_tracker_base_url(product),
            headers=self._get_headers(product),
            data={
                "search": correlation_id,
                "in": "description",
                "state": "all",
            },
        )

        for item in response.json() or []:
            if correlation_id in (item.get("description") or ""):
                return str(item.get("iid"))

        return None

    def get_frontend_issue_url(self, product: Product, issue_id: str) -> str:
        base_url = self._normalize_base_url(product.issue_tracker_base_url)
        return f"{base_url}/{product.issue_tracker_project_id}/-/issues/{issue_id}"

    def _get_state(self, state: Optional[str]) -> str:
        if state == "closed":
            return Issue_State.ISSUE_STATE_CLOSED
        return Issue_State.ISSUE_STATE_OPEN

    def _get_issue_tracker_base_url(self, product: Product) -> str:
        base_url = self._normalize_base_url(product.issue_tracker_base_url)
        return f"{base_url}/api/v4/projects/{urllib.parse.quote_plus(product.issue_tracker_project_id)}/issues"

    def _get_headers(self, product: Product) -> dict:
        return {
            "PRIVATE-TOKEN": product.issue_tracker_api_key,
        }
