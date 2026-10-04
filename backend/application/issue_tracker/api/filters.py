from django_filters import ChoiceFilter, FilterSet, OrderingFilter

from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.types import Desired_State, Sync_State


class IssueTrackerStatusFilter(FilterSet):
    sync_state = ChoiceFilter(field_name="sync_state", choices=Sync_State.SYNC_STATE_CHOICES)
    desired_state = ChoiceFilter(field_name="desired_state", choices=Desired_State.DESIRED_STATE_CHOICES)

    ordering = OrderingFilter(
        fields=(
            ("id", "id"),
            ("product", "product"),
            ("desired_state", "desired_state"),
            ("sync_state", "sync_state"),
            ("next_attempt_at", "next_attempt_at"),
            ("modified", "modified"),
        ),
    )

    class Meta:
        model = Issue_Tracker_Status
        fields = ["product", "sync_state", "desired_state"]
