import logging
from typing import Any

import requests

from application.issue_tracker.issue_trackers.base_issue_tracker import (
    IssueTrackerDeterministicError,
    IssueTrackerUncertainError,
)

logger = logging.getLogger("secobserve.issue_tracker")

# HTTP status codes where the outcome at the remote side is unknown: the request may
# have been executed even though the response could not be delivered/processed.
UNCERTAIN_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}

DEFAULT_TIMEOUT = 60


def request_issue_tracker(method: str, url: str, **kwargs: Any) -> requests.Response:
    """
    Performs an HTTP request against an issue tracker and converts every error into an
    IssueTrackerUncertainError (the remote state is unknown, query before retrying) or an
    IssueTrackerDeterministicError (the request definitely failed).
    """
    try:
        response = requests.request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs)
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        raise IssueTrackerUncertainError(str(e)) from e
    except requests.exceptions.RequestException as e:
        raise IssueTrackerDeterministicError(str(e)) from e

    if response.status_code == 404:
        return response
    if response.status_code >= 400:
        message = f"{response.status_code} {response.reason} for url: {url}"
        if response.status_code in UNCERTAIN_STATUS_CODES:
            raise IssueTrackerUncertainError(message)
        raise IssueTrackerDeterministicError(message)

    return response
