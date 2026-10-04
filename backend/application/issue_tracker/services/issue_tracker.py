from typing import Iterable, Optional

from application.access_control.models import User
from application.core.models import Observation, Product
from application.issue_tracker.services.issue_sync_registration import (
    register_observation_sync,
)
from application.issue_tracker.services.tracker_factory import issue_tracker_factory

__all__ = [
    "push_observations_to_issue_tracker",
    "push_observation_to_issue_tracker",
    "issue_tracker_factory",
]


def push_observations_to_issue_tracker(product: Product, observations: Iterable[Observation]) -> None:
    """
    Registers the desired state for the given observations in the business transaction.
    No remote communication happens here.
    """
    if product.issue_tracker_active:
        for observation in observations:
            push_observation_to_issue_tracker(observation, None)


def push_observation_to_issue_tracker(observation: Observation, user: Optional[User]) -> None:
    # The user is kept in the signature for the call sites; background notifications are
    # sent without a user context.
    del user
    product = observation.product
    if product.issue_tracker_active and observation.branch == product.repository_default_branch:
        register_observation_sync(observation)
