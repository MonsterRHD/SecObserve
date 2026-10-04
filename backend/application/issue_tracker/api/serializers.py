from rest_framework.serializers import ModelSerializer, SerializerMethodField

from application.issue_tracker.models import Issue_Tracker_Status


class IssueTrackerStatusSerializer(ModelSerializer):
    product_name = SerializerMethodField()

    class Meta:
        model = Issue_Tracker_Status
        fields = [
            "id",
            "product",
            "product_name",
            "observation",
            "issue_id",
            "desired_state",
            "revision",
            "sync_state",
            "create_uncertain",
            "attempts",
            "locked_at",
            "last_attempt_at",
            "next_attempt_at",
            "last_error",
            "created",
            "modified",
        ]

    def get_product_name(self, status: Issue_Tracker_Status) -> str:
        return status.product.name
