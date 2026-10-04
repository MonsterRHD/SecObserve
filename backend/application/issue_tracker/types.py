class Issue_Tracker:
    ISSUE_TRACKER_GITHUB = "GitHub"
    ISSUE_TRACKER_GITLAB = "GitLab"
    ISSUE_TRACKER_JIRA = "Jira"

    ISSUE_TRACKER_TYPE_CHOICES = [
        (ISSUE_TRACKER_GITHUB, ISSUE_TRACKER_GITHUB),
        (ISSUE_TRACKER_GITLAB, ISSUE_TRACKER_GITLAB),
        (ISSUE_TRACKER_JIRA, ISSUE_TRACKER_JIRA),
    ]


class Desired_State:
    # The issue in the external issue tracker must exist and be open.
    DESIRED_STATE_OPEN = "OPEN"
    # The issue in the external issue tracker must be closed.
    DESIRED_STATE_CLOSED = "CLOSED"
    # The observation has been deleted, the issue must get a deletion note and be closed.
    DESIRED_STATE_DELETED = "DELETED"

    DESIRED_STATE_CHOICES = [
        (DESIRED_STATE_OPEN, DESIRED_STATE_OPEN),
        (DESIRED_STATE_CLOSED, DESIRED_STATE_CLOSED),
        (DESIRED_STATE_DELETED, DESIRED_STATE_DELETED),
    ]


class Sync_State:
    # A new desired revision has been registered and waits for convergence.
    SYNC_STATE_PENDING = "PENDING"
    # A worker has claimed the row and is currently talking to the remote system.
    SYNC_STATE_IN_PROGRESS = "IN_PROGRESS"
    # The remote system reflects the desired state.
    SYNC_STATE_SUCCEEDED = "SUCCEEDED"
    # The automatic retries are exhausted or a deterministic error occurred.
    SYNC_STATE_FAILED = "FAILED"

    SYNC_STATE_CHOICES = [
        (SYNC_STATE_PENDING, SYNC_STATE_PENDING),
        (SYNC_STATE_IN_PROGRESS, SYNC_STATE_IN_PROGRESS),
        (SYNC_STATE_SUCCEEDED, SYNC_STATE_SUCCEEDED),
        (SYNC_STATE_FAILED, SYNC_STATE_FAILED),
    ]
