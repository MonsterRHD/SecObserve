from typing import Any, Optional

import requests
from jira.client import JIRA
from jira.exceptions import JIRAError
from jira.resources import Issue as JiraIssue

from application.commons.services.functions import get_base_url_frontend
from application.core.models import Observation, Product
from application.issue_tracker.issue_trackers.base_issue_tracker import (
    BaseIssueTracker,
    Issue,
    Issue_State,
    IssueTrackerDeterministicError,
    IssueTrackerError,
    IssueTrackerUncertainError,
)
from application.issue_tracker.issue_trackers.request_helper import (
    UNCERTAIN_STATUS_CODES,
)


class JiraIssueTracker(BaseIssueTracker):
    def __init__(self, product: Product, with_communication: bool = True) -> None:
        if with_communication:
            self.jira = JIRA(
                server=product.issue_tracker_base_url,
                basic_auth=(
                    product.issue_tracker_username,
                    product.issue_tracker_api_key,
                ),
            )

    def create_issue(self, observation: Observation) -> str:
        labels = observation.product.issue_tracker_labels.split(",")

        try:
            jira_issue = self.jira.create_issue(
                project=observation.product.issue_tracker_project_id,
                summary=self._get_title(observation),
                description=self._get_description(observation),
                labels=[item.strip() for item in labels],
                issuetype=observation.product.issue_tracker_issue_type,
            )
        except JIRAError as e:
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise IssueTrackerUncertainError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise IssueTrackerDeterministicError(str(e)) from e

        return jira_issue.key

    def get_issue(self, product: Product, issue_id: str) -> Optional[Issue]:
        jira_issue = self._get_jira_issue(issue_id)
        if not jira_issue:
            return None

        return Issue(
            id=issue_id,
            title=jira_issue.fields.summary,
            description=jira_issue.fields.description,
            labels=",".join(jira_issue.fields.labels),
            state=self._get_state(product, jira_issue),
            jira_initial_status=str(jira_issue.fields.status),
        )

    def update_issue(self, observation: Observation, issue: Issue) -> Optional[str]:
        jira_issue = self._get_jira_issue(issue.id)
        if not jira_issue:
            return None

        try:
            jira_issue.update(
                summary=self._get_title(observation),
                description=self._get_description(observation),
            )

            if self._get_state(observation.product, jira_issue) == Issue_State.ISSUE_STATE_CLOSED:
                target_status = observation.issue_tracker_jira_initial_status or self._get_reopen_status(
                    observation.product, jira_issue
                )
                if target_status:
                    self.jira.transition_issue(jira_issue, target_status)
                return target_status
        except JIRAError as e:
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise IssueTrackerUncertainError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise IssueTrackerDeterministicError(str(e)) from e

        return str(jira_issue.fields.status)

    def close_issue(self, observation: Observation, issue: Issue) -> None:
        description = self._get_description(observation)
        description += f"\n\n*Observation status:* {observation.current_status}"

        jira_issue = self._get_jira_issue(issue.id)
        if not jira_issue:
            return

        try:
            jira_issue.update(
                summary=self._get_title(observation),
                description=description,
            )

            # Idempotent close: do not repeat the transition when the issue is already
            # in the configured closed status.
            if str(jira_issue.fields.status) != observation.product.issue_tracker_status_closed:
                self.jira.transition_issue(jira_issue, observation.product.issue_tracker_status_closed)
        except JIRAError as e:
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise IssueTrackerUncertainError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise IssueTrackerDeterministicError(str(e)) from e

    def close_issue_for_deleted_observation(self, product: Product, issue: Issue) -> None:
        jira_issue = self._get_jira_issue(issue.id)
        if not jira_issue:
            return

        try:
            if self._already_has_deletion_note(issue) or self._has_jira_deletion_note(jira_issue):
                description = jira_issue.fields.description
            else:
                description = self._get_description_for_deleted_observation(jira_issue.fields.description)
                description = description.replace("**", "*")

            jira_issue.update(
                summary=jira_issue.fields.summary,
                description=description,
            )

            if str(jira_issue.fields.status) != product.issue_tracker_status_closed:
                self.jira.transition_issue(jira_issue, product.issue_tracker_status_closed)
        except JIRAError as e:
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise IssueTrackerUncertainError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise IssueTrackerDeterministicError(str(e)) from e

    def find_issue_id_by_correlation_id(self, product: Product, correlation_id: str) -> Optional[str]:
        jql = f'project = "{product.issue_tracker_project_id}" AND text ~ "{correlation_id}"'
        try:
            issues = self.jira.search_issues(jql, fields="summary,description,labels,status", maxResults=10)
        except JIRAError as e:
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise IssueTrackerUncertainError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise IssueTrackerDeterministicError(str(e)) from e

        for jira_issue in issues or []:
            description = getattr(jira_issue.fields, "description", None) or ""
            if correlation_id in description:
                return jira_issue.key

        return None

    def get_frontend_issue_url(self, product: Product, issue_id: str) -> str:
        return f"{product.issue_tracker_base_url}/browse/{issue_id}"

    def get_correlation_marker(self, observation: Observation) -> str:
        # Jira does not render HTML comments, use a grey colored text instead.
        token = self.get_correlation_token(observation)
        return f"{{color:#888888}}{token}{{color}}"

    def get_correlation_marker_for_id(self, observation_id: Optional[int]) -> str:
        return f"{{color:#888888}}secobserveobservation{observation_id}{{color}}"

    def _has_jira_deletion_note(self, jira_issue: JiraIssue) -> bool:
        description = getattr(jira_issue.fields, "description", None) or ""
        return "*--- Observation has been deleted ---*" in description

    def _get_description(self, observation: Observation) -> str:
        description = observation.description
        description = description.replace("**", "*")
        description = description.replace("```", "{code}")

        if observation.branch:
            description += f"\n\n*Branch:* {observation.branch.name}"

        url = f"{get_base_url_frontend()}#/observations/{observation.pk}/show"
        description += f"\n\n*SecObserve observation:* {url}"

        description += f"\n\n{self.get_correlation_marker(observation)}"

        return description

    def _get_jira_issue(self, issue_id: str) -> Optional[JiraIssue]:
        try:
            return self.jira.issue(issue_id, fields="summary,description,labels,status")
        except JIRAError as e:
            if e.status_code == 404:
                return None
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e2:
            raise IssueTrackerUncertainError(str(e2)) from e2
        except requests.exceptions.RequestException as e2:
            raise IssueTrackerDeterministicError(str(e2)) from e2

    def _get_state(self, product: Product, jira_issue: JiraIssue) -> str:
        if str(jira_issue.fields.status) == product.issue_tracker_status_closed:
            return Issue_State.ISSUE_STATE_CLOSED
        return Issue_State.ISSUE_STATE_OPEN

    def _get_reopen_status(self, product: Product, jira_issue: JiraIssue) -> Optional[str]:
        try:
            transitions = self.jira.transitions(jira_issue) or []
        except JIRAError as e:
            raise self._convert_jira_error(e) from e
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            raise IssueTrackerUncertainError(str(e)) from e
        except requests.exceptions.RequestException as e:
            raise IssueTrackerDeterministicError(str(e)) from e

        # Prefer a transition into a status whose category is not "done".
        for transition in transitions:
            to_status = self._get_transition_to_status(transition)
            category_key = str((to_status.get("statusCategory") or {}).get("key") or "")
            if category_key and category_key != "done":
                return str(to_status.get("name"))
        # Fallback: the first target status that is not the configured closed status.
        for transition in transitions:
            name = self._get_transition_to_status(transition).get("name")
            if name and name != product.issue_tracker_status_closed:
                return str(name)
        # Last resort for standard Jira workflows.
        return "Open"

    def _get_transition_to_status(self, transition: Any) -> dict:
        to_status = transition.get("to", {}) if isinstance(transition, dict) else {}
        return to_status if isinstance(to_status, dict) else {}

    def _convert_jira_error(self, error: JIRAError) -> IssueTrackerError:
        message = f"Jira error {error.status_code}: {error.text}"
        if error.status_code in UNCERTAIN_STATUS_CODES:
            return IssueTrackerUncertainError(message)
        return IssueTrackerDeterministicError(message)
