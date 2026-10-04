from dataclasses import dataclass
from typing import Optional

from application.commons.services.functions import get_base_url_frontend
from application.core.models import Observation, Product


class Issue_State:
    ISSUE_STATE_OPEN = "open"
    ISSUE_STATE_CLOSED = "closed"


@dataclass
class Issue:
    id: str
    title: str
    description: Optional[str] = None
    labels: Optional[str] = None
    state: str = Issue_State.ISSUE_STATE_OPEN
    # Only used by Jira: the status the issue had directly after its creation, needed to
    # reopen it without knowing the workflow configuration.
    jira_initial_status: Optional[str] = None


class IssueTrackerError(Exception):
    """Base class for all errors raised while communicating with an issue tracker."""


class IssueTrackerUncertainError(IssueTrackerError):
    """
    The remote system may or may not have executed the request (timeout, connection
    error, 408/409/429 or 5xx). The request must not simply be repeated: the actual
    remote state has to be queried first.
    """


class IssueTrackerDeterministicError(IssueTrackerError):
    """
    The request definitely failed and repeating it without a change (e.g. a new
    revision) makes no sense (configuration error or 4xx other than 408/409/429).
    """


DELETION_NOTE_HEADER = "**--- Observation has been deleted ---**"


class BaseIssueTracker:
    def create_issue(self, observation: Observation) -> str:
        raise NotImplementedError("create_issue() must be overridden")

    def get_issue(self, product: Product, issue_id: str) -> Optional[Issue]:
        raise NotImplementedError("get_issue() must be overridden")

    def update_issue(self, observation: Observation, issue: Issue) -> None:
        raise NotImplementedError("update_issue() must be overridden")

    def close_issue(self, observation: Observation, issue: Issue) -> None:
        raise NotImplementedError("close_issue() must be overridden")

    def close_issue_for_deleted_observation(self, product: Product, issue: Issue) -> None:
        raise NotImplementedError("close_issue_for_deleted_observation() must be overridden")

    def find_issue_id_by_correlation_id(self, product: Product, correlation_id: str) -> Optional[str]:
        """
        Find an issue that was created from SecObserve for an observation whose local
        issue id is unknown, e.g. after an uncertain create response. Returns the issue
        id or None. Implementations must verify that the correlation marker is actually
        part of the issue description, so that fuzzy search results cannot be adopted.
        """
        raise NotImplementedError("find_issue_id_by_correlation_id() must be overridden")

    def get_frontend_issue_url(self, product: Product, issue_id: str) -> str:
        raise NotImplementedError("get_frontend_issue_url() must be overridden")

    def get_correlation_token(self, observation: Observation) -> str:
        return f"secobserveobservation{observation.pk}"

    def get_correlation_marker(self, observation: Observation) -> str:
        return f"<!-- {self.get_correlation_token(observation)} -->"

    def get_correlation_marker_for_id(self, observation_id: Optional[int]) -> str:
        return f"<!-- secobserveobservation{observation_id} -->"

    def _get_title(self, observation: Observation) -> str:
        title = f'{observation.current_severity} vulnerability: "{observation.title}"'
        origin = self._get_origin(observation)
        if origin:
            title += f" in {origin}"
        return title

    def _get_description(self, observation: Observation) -> str:
        description = observation.description

        if observation.branch:
            description += f"\n\n**Branch:** {observation.branch.name}"

        url = f"{get_base_url_frontend()}#/observations/{observation.pk}/show"
        description += f"\n\n**SecObserve observation:** [{url}]({url})"

        # Hidden correlation marker so that an issue can be found after an uncertain
        # create response without creating a duplicate.
        description += f"\n\n{self.get_correlation_marker(observation)}"

        return description

    def _get_description_for_deleted_observation(self, description: Optional[str]) -> str:
        return f"{DELETION_NOTE_HEADER}\n\n{description}"

    def _already_has_deletion_note(self, issue: Issue) -> bool:
        return bool(issue.description and DELETION_NOTE_HEADER in issue.description)

    def _get_origin(self, observation: Observation) -> str:
        origin = ""
        if observation.origin_service:
            origin += f"{observation.origin_service.name}"
        if observation.origin_component_name_version:
            if origin:
                origin += " / "
            origin += f"{observation.origin_component_name_version}"
        if observation.origin_docker_image_name_tag_short:
            if origin:
                origin += " / "
            origin += f"{observation.origin_docker_image_name_tag_short}"
        if origin_endpoint_hostname := getattr(observation, "origin_endpoint_hostname", ""):
            if origin:
                origin += " / "
            origin += origin_endpoint_hostname
        if observation.origin_source_file:
            if origin:
                origin += " / "
            origin += f"{observation.origin_source_file}"
        if observation.origin_cloud_provider:
            if origin:
                origin += " / "
            origin += f"{observation.origin_cloud_provider} / "
            origin += f"{observation.origin_cloud_account_subscription_project} / "
            origin += f"{observation.origin_cloud_resource}"
        return origin

    def _normalize_base_url(self, base_url: str) -> str:
        if base_url.endswith("/"):
            base_url = base_url[:-1]
        return base_url
