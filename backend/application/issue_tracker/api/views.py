from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.status import HTTP_202_ACCEPTED, HTTP_409_CONFLICT
from rest_framework.viewsets import GenericViewSet

from application.authorization.services.authorization import user_has_permission_or_403
from application.authorization.services.roles_permissions import Permissions
from application.core.queries.product import get_products
from application.issue_tracker.api.filters import IssueTrackerStatusFilter
from application.issue_tracker.api.serializers import IssueTrackerStatusSerializer
from application.issue_tracker.models import Issue_Tracker_Status
from application.issue_tracker.services.issue_sync_registration import (
    reset_for_manual_retry,
)


class IssueTrackerStatusViewSet(GenericViewSet, ListModelMixin, RetrieveModelMixin):
    serializer_class = IssueTrackerStatusSerializer
    filterset_class = IssueTrackerStatusFilter
    permission_classes = [IsAuthenticated]
    filter_backends = [SearchFilter, DjangoFilterBackend]
    search_fields = ["issue_id", "last_error", "product__name"]
    queryset = Issue_Tracker_Status.objects.none()

    def get_queryset(self):
        return Issue_Tracker_Status.objects.select_related("product").filter(product__in=get_products()).order_by("-id")

    @extend_schema(
        methods=["POST"],
        request=None,
        responses={HTTP_202_ACCEPTED: None, HTTP_409_CONFLICT: None},
    )
    @action(detail=True, methods=["post"])
    def retry(self, request: Request, pk: int = None) -> Response:  # pylint: disable=unused-argument
        status_row = self.get_object()
        user_has_permission_or_403(status_row.product, Permissions.Product_Edit)

        if reset_for_manual_retry(status_row):
            return Response(status=HTTP_202_ACCEPTED)
        return Response(
            {"detail": "Only failed synchronizations or synchronizations waiting for retry can be retried"},
            status=HTTP_409_CONFLICT,
        )
