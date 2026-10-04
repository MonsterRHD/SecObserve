from typing import Any, Optional

from dirtyfields import DirtyFieldsMixin
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import (
    CASCADE,
    PROTECT,
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    Index,
    IntegerField,
    Manager,
    ManyToManyField,
    Model,
    PositiveIntegerField,
    QuerySet,
    TextField,
    UniqueConstraint,
)
from django.utils import timezone

from application.access_control.models import Authorization_Group, User
from application.core.models import Branch, Component, Product, Service
from application.licenses.types import (
    NO_LICENSE_INFORMATION,
    Component_Type,
    License_Policy_Evaluation_Result,
)


class License_Generation(Model):
    """
    An identifiable generation of the SPDX license directory.

    Only one generation at a time has the status CURRENT. A new generation is
    prepared (status PREPARING) with its own set of License rows and becomes
    CURRENT in a single transaction. Readers always see the licenses of the
    CURRENT generation only, so a refresh that fails in any step leaves the
    previous complete generation untouched.
    """

    STATUS_PREPARING = "preparing"
    STATUS_CURRENT = "current"
    STATUS_SUPERSEDED = "superseded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PREPARING, STATUS_PREPARING),
        (STATUS_CURRENT, STATUS_CURRENT),
        (STATUS_SUPERSEDED, STATUS_SUPERSEDED),
        (STATUS_FAILED, STATUS_FAILED),
    ]

    TRIGGER_INITIAL = "initial"
    TRIGGER_REFRESH = "refresh"

    sequence = PositiveIntegerField(unique=True)
    status = CharField(max_length=16, choices=STATUS_CHOICES)
    trigger = CharField(max_length=16, blank=True, default="")
    license_count = PositiveIntegerField(default=0)
    checksum = CharField(max_length=64, blank=True, default="")
    spdx_release_date = CharField(max_length=32, blank=True, default="")
    error_message = CharField(max_length=2048, blank=True, default="")
    started_at = DateTimeField(auto_now_add=True)
    published_at = DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [Index(fields=["status"])]
        ordering = ["-sequence"]

    def __str__(self) -> str:
        return f"License generation {self.sequence} ({self.status})"


def _get_default_license_generation_pk() -> Optional[int]:
    # Used as the default of License.generation so that objects created without
    # an explicit generation (e.g. via loaddata) are attached to the current
    # generation. Imported as a function to avoid circular model imports.
    try:
        from application.licenses.queries.license_generation import (
            get_current_generation_pk,
        )

        return get_current_generation_pk()
    except Exception:  # pylint: disable=broad-except
        # The database may not be available/migrated yet (model construction at
        # import time). A missing generation is enforced by NOT NULL at save.
        return None


class LicenseQuerySet(QuerySet):
    def current(self) -> "LicenseQuerySet":
        return self.filter(generation__status=License_Generation.STATUS_CURRENT)

    def for_generation(self, generation_id: int) -> "LicenseQuerySet":
        return self.filter(generation_id=generation_id)


class LicenseManager(Manager):
    def get_queryset(self) -> LicenseQuerySet:
        return LicenseQuerySet(self.model, using=self._db)

    def current(self) -> LicenseQuerySet:
        return self.get_queryset().current()

    def for_generation(self, generation_id: int) -> LicenseQuerySet:
        return self.get_queryset().for_generation(generation_id)


class License(Model):
    objects = LicenseManager()

    spdx_id = CharField(max_length=255)
    name = CharField(max_length=255)
    reference = TextField(max_length=2048, blank=True)
    is_osi_approved = BooleanField(null=True)
    is_deprecated = BooleanField(null=True)

    generation = ForeignKey(
        License_Generation,
        related_name="licenses",
        on_delete=PROTECT,
        default=_get_default_license_generation_pk,
    )
    is_custom = BooleanField(default=False)

    class Meta:
        constraints = [
            UniqueConstraint(fields=["spdx_id", "generation"], name="unique_spdx_id_per_generation"),
        ]
        indexes = [Index(fields=["name"])]

    def __str__(self) -> str:
        return self.spdx_id


class License_Group(Model):
    name = CharField(max_length=255, unique=True)
    description = TextField(max_length=2048, blank=True)
    is_public = BooleanField(default=False)
    licenses = ManyToManyField(License, related_name="license_groups")
    users: ManyToManyField = ManyToManyField(
        User, through="License_Group_Member", related_name="license_groups", blank=True
    )
    authorization_groups: ManyToManyField = ManyToManyField(
        Authorization_Group,
        through="License_Group_Authorization_Group_Member",
        related_name="license_groups",
        blank=True,
    )

    def __str__(self) -> str:
        return self.name


class License_Group_Member(Model):
    license_group = ForeignKey(License_Group, related_name="license_group_members", on_delete=CASCADE)
    user = ForeignKey(User, related_name="license_group_members", on_delete=CASCADE)
    is_manager = BooleanField(default=False)

    class Meta:
        unique_together = ("license_group", "user")

    def __str__(self) -> str:
        return f"{self.license_group} / {self.user}"


class License_Group_Authorization_Group_Member(Model):
    license_group = ForeignKey(
        License_Group, related_name="license_group_authorization_group_members", on_delete=CASCADE
    )
    authorization_group = ForeignKey(
        Authorization_Group, related_name="license_group_authorization_group_members", on_delete=CASCADE
    )
    is_manager = BooleanField(default=False)

    class Meta:
        unique_together = ("license_group", "authorization_group")

    def __str__(self) -> str:
        return f"{self.license_group} / {self.authorization_group}"


class License_ComponentManager(Manager["License_Component"]):
    def get_queryset(self) -> QuerySet["License_Component"]:
        # License components are queried from many places, most of which do not add their own
        # select_related. The component is joined centrally here to avoid N+1 queries.
        return super().get_queryset().select_related("component")


class License_Component(Model, DirtyFieldsMixin):
    objects = License_ComponentManager()

    identity_hash = CharField(max_length=64)

    product = ForeignKey(Product, related_name="license_components", on_delete=CASCADE)
    branch = ForeignKey(Branch, related_name="license_components", on_delete=CASCADE, null=True)
    upload_filename = CharField(max_length=255, blank=True)

    component = ForeignKey(Component, on_delete=SET_NULL, null=True)
    component_name = CharField(max_length=255)
    component_version = CharField(max_length=255, blank=True)
    component_name_version = CharField(max_length=513, blank=True)
    component_type = CharField(max_length=24, choices=Component_Type.TYPE_CHOICES, blank=True)
    component_purl = CharField(max_length=255, blank=True)
    component_purl_type = CharField(max_length=16, blank=True)
    component_cpe = CharField(max_length=255, blank=True)
    component_dependencies = TextField(max_length=32768, blank=True)
    component_cyclonedx_bom_link = CharField(max_length=512, blank=True)

    imported_declared_license_name = CharField(max_length=255, blank=True, default=NO_LICENSE_INFORMATION)
    imported_declared_spdx_license = ForeignKey(
        License, related_name="imported_declared_license_components", on_delete=PROTECT, blank=True, null=True
    )
    imported_declared_license_expression = CharField(max_length=255, blank=True)
    imported_declared_non_spdx_license = CharField(max_length=255, blank=True)
    imported_declared_multiple_licenses = CharField(max_length=512, blank=True)

    imported_concluded_license_name = CharField(max_length=255, blank=True, default=NO_LICENSE_INFORMATION)
    imported_concluded_spdx_license = ForeignKey(
        License, related_name="imported_concluded_license_components", on_delete=PROTECT, blank=True, null=True
    )
    imported_concluded_license_expression = CharField(max_length=255, blank=True)
    imported_concluded_non_spdx_license = CharField(max_length=255, blank=True)
    imported_concluded_multiple_licenses = CharField(max_length=512, blank=True)

    manual_concluded_license_name = CharField(max_length=255, blank=True, default=NO_LICENSE_INFORMATION)
    manual_concluded_spdx_license = ForeignKey(
        License, related_name="manual_concluded_license_components", on_delete=PROTECT, blank=True, null=True
    )
    manual_concluded_license_expression = CharField(max_length=255, blank=True)
    manual_concluded_non_spdx_license = CharField(max_length=255, blank=True)
    manual_concluded_comment = CharField(max_length=255, blank=True)

    effective_license_name = CharField(max_length=255, blank=True, default=NO_LICENSE_INFORMATION)
    effective_spdx_license = ForeignKey(
        License, related_name="effective_license_components", on_delete=PROTECT, blank=True, null=True
    )
    effective_license_expression = CharField(max_length=255, blank=True)
    effective_non_spdx_license = CharField(max_length=255, blank=True)
    effective_multiple_licenses = CharField(max_length=512, blank=True)

    evaluation_result = CharField(max_length=16, choices=License_Policy_Evaluation_Result.RESULT_CHOICES, blank=True)
    numerical_evaluation_result = IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])

    origin_service = ForeignKey(Service, on_delete=RESTRICT, null=True)

    created = DateTimeField(auto_now_add=True)
    import_last_seen = DateTimeField(default=timezone.now)
    last_change = DateTimeField(default=timezone.now)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        self.unsaved_declared_licenses: list[str] = []
        self.unsaved_concluded_licenses: list[str] = []
        self.unsaved_evidences: list[list[str]] = []

    def __str__(self) -> str:
        return self.component_name_version

    def __setattr__(self, attrname: str, val: Any) -> None:
        super().__setattr__(attrname, val)

        if attrname == "evaluation_result":
            self.numerical_evaluation_result = License_Policy_Evaluation_Result.NUMERICAL_RESULTS.get(val, 3)


class License_Component_Evidence(Model):
    license_component = ForeignKey(License_Component, related_name="evidences", on_delete=CASCADE)
    name = CharField(max_length=255)
    evidence = TextField()

    class Meta:
        indexes = [Index(fields=["name"])]


class Concluded_License(Model):
    product = ForeignKey(Product, related_name="concluded_licenses", on_delete=CASCADE)
    component_purl_type = CharField(max_length=16, blank=True)
    component_name = CharField(max_length=255)
    component_version = CharField(max_length=255, blank=True)

    manual_concluded_spdx_license = ForeignKey(
        License, related_name="manual_concluded_licenses", on_delete=CASCADE, blank=True, null=True
    )
    manual_concluded_license_expression = CharField(max_length=255, blank=True)
    manual_concluded_non_spdx_license = CharField(max_length=255, blank=True)

    user = ForeignKey(User, related_name="concluded_licenses", on_delete=PROTECT)
    last_updated = DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("product", "component_purl_type", "component_name", "component_version")


class License_Policy(Model):
    parent = ForeignKey("self", on_delete=PROTECT, related_name="children", null=True, blank=True)
    name = CharField(max_length=255, unique=True)
    description = TextField(max_length=2048, blank=True)
    is_public = BooleanField(default=False)
    ignore_component_types = CharField(max_length=255, blank=True)
    users: ManyToManyField = ManyToManyField(
        User, through="License_Policy_Member", related_name="license_policies", blank=True
    )
    authorization_groups: ManyToManyField = ManyToManyField(
        Authorization_Group,
        through="License_Policy_Authorization_Group_Member",
        related_name="license_policies",
        blank=True,
    )

    def __str__(self) -> str:
        return self.name


class License_Policy_Item(Model):
    license_policy = ForeignKey(License_Policy, related_name="license_policy_items", on_delete=CASCADE)
    license_group = ForeignKey(
        License_Group, related_name="license_policy_items", on_delete=PROTECT, blank=True, null=True
    )
    license = ForeignKey(License, related_name="license_policy_items", on_delete=PROTECT, blank=True, null=True)
    license_expression = CharField(max_length=255, blank=True)
    non_spdx_license = CharField(max_length=255, blank=True)
    evaluation_result = CharField(max_length=16, choices=License_Policy_Evaluation_Result.RESULT_CHOICES)
    numerical_evaluation_result = IntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = CharField(max_length=255, blank=True)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.numerical_evaluation_result = License_Policy_Evaluation_Result.NUMERICAL_RESULTS.get(
            self.evaluation_result, License_Policy_Evaluation_Result.RESULT_UNKNOWN
        )

        return super().save(*args, **kwargs)


class License_Policy_Member(Model):
    license_policy = ForeignKey(License_Policy, related_name="license_policy_members", on_delete=CASCADE)
    user = ForeignKey(User, related_name="license_policy_members", on_delete=CASCADE)
    is_manager = BooleanField(default=False)

    class Meta:
        unique_together = ("license_policy", "user")

    def __str__(self) -> str:
        return f"{self.license_policy} / {self.user}"


class License_Policy_Authorization_Group_Member(Model):
    license_policy = ForeignKey(
        License_Policy, related_name="license_policy_authorization_group_members", on_delete=CASCADE
    )
    authorization_group = ForeignKey(
        Authorization_Group, related_name="license_policy_authorization_group_members", on_delete=CASCADE
    )
    is_manager = BooleanField(default=False)

    class Meta:
        unique_together = ("license_policy", "authorization_group")

    def __str__(self) -> str:
        return f"{self.license_policy} / {self.authorization_group}"
