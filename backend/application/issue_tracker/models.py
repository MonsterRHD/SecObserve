from django.db import models

from application.core.models import Observation, Product
from application.issue_tracker.types import Desired_State, Sync_State


class Issue_Tracker_Status(models.Model):
    """
    Outbox and convergence state of the synchronization of one observation with
    its issue in the external issue tracker.

    Business transactions only register the desired state (OPEN/CLOSED/DELETED) and
    bump the revision. Background tasks claim the row with a lease and converge the
    remote system to the latest registered revision. Rows for deleted observations
    are kept until the deletion note and the close operation have been confirmed.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="issue_tracker_statuses",
    )
    observation = models.OneToOneField(
        Observation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issue_tracker_status",
    )

    # Correlation marker secobserveobservation{observation_id}, kept separately so that
    # an issue can be found even when the observation has already been deleted.
    correlation_id = models.CharField(max_length=64, blank=True)
    issue_id = models.CharField(max_length=255, blank=True)
    desired_state = models.CharField(max_length=12, choices=Desired_State.DESIRED_STATE_CHOICES)
    revision = models.PositiveBigIntegerField(default=1)

    sync_state = models.CharField(
        max_length=12,
        choices=Sync_State.SYNC_STATE_CHOICES,
        default=Sync_State.SYNC_STATE_PENDING,
        db_index=True,
    )
    # True after a create call had an uncertain outcome (timeout, connection error, 5xx).
    # The next runs search the issue by its correlation marker before they create anything,
    # no matter what the desired state is. The flag is kept across revisions and manual retries.
    create_uncertain = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)

    locked_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["sync_state", "next_attempt_at"]),
            models.Index(fields=["product"]),
            models.Index(fields=["-revision"]),
            models.Index(fields=["correlation_id"]),
        ]
