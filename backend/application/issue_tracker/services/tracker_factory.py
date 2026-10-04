from application.core.models import Product
from application.issue_tracker.issue_trackers.base_issue_tracker import BaseIssueTracker
from application.issue_tracker.issue_trackers.github_issue_tracker import (
    GitHubIssueTracker,
)
from application.issue_tracker.issue_trackers.gitlab_issue_tracker import (
    GitLabIssueTracker,
)
from application.issue_tracker.issue_trackers.jira_issue_tracker import JiraIssueTracker
from application.issue_tracker.types import Issue_Tracker


def issue_tracker_factory(product: Product, with_communication: bool = True) -> BaseIssueTracker:
    if product.issue_tracker_type == Issue_Tracker.ISSUE_TRACKER_GITHUB:
        return GitHubIssueTracker()

    if product.issue_tracker_type == Issue_Tracker.ISSUE_TRACKER_GITLAB:
        return GitLabIssueTracker()

    if product.issue_tracker_type == Issue_Tracker.ISSUE_TRACKER_JIRA:
        return JiraIssueTracker(product=product, with_communication=with_communication)

    raise ValueError(f"Unknown issue tracker type: {product.issue_tracker_type}")
