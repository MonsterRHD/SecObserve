import json
import random
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from application.access_control.models import User
from application.core.models import Product
from application.licenses.models import (
    Concluded_License,
    License,
    License_Component,
    License_Generation,
    License_Group,
    License_Policy,
    License_Policy_Item,
)
from application.licenses.types import License_Policy_Evaluation_Result
from application.licenses.services.license import import_licenses
from application.licenses.services.license_generation import (
    LicenseRefreshValidationError,
    cleanup_stale_generations,
    publish_prepared_generation,
)
from application.licenses.services.spdx_license_cache import SPDXLicenseCache
from unittests.base_test_case import BaseTestCase


def _spdx_entry(
    license_id: str,
    name: str = "",
    reference: str = "",
    is_osi_approved: bool = True,
    is_deprecated: bool = False,
) -> dict:
    return {
        "licenseId": license_id,
        "name": name or f"{license_id} name",
        "reference": reference or f"https://example.com/{license_id}",
        "isOsiApproved": is_osi_approved,
        "isDeprecatedLicenseId": is_deprecated,
    }


def _payload(entries: list[dict], release_date: str = "2026-01-01") -> bytes:
    return json.dumps({"releaseDate": release_date, "licenses": entries}).encode("utf-8")


def _mock_response(content: bytes):
    response = patch("application.licenses.services.license_generation.requests").start().get.return_value
    response.content = content
    response.raise_for_status.return_value = None
    return response


class TestLicenseGenerationRefresh(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Unit tests run with migrations disabled, so the baseline generation
        # created by migration 0025 has to be set up explicitly. Some test
        # classes in this suite load fixtures in a way that their data is not
        # rolled back, therefore fixture generations may be visible here. Move
        # them out of the way and use a generation with a random sequence to
        # make these tests independent from that leaked data.
        License_Generation.objects.filter(status=License_Generation.STATUS_CURRENT).update(
            status=License_Generation.STATUS_SUPERSEDED
        )
        self.baseline_generation = License_Generation.objects.create(
            sequence=random.randint(1_000_000, 2_000_000_000),
            status=License_Generation.STATUS_CURRENT,
            license_count=3,
        )

        self.mit_old = License.objects.create(
            spdx_id="MIT",
            name="MIT license old name",
            reference="https://example.com/MIT-old",
            is_osi_approved=True,
            is_deprecated=False,
            generation=self.baseline_generation,
        )
        self.apache_old = License.objects.create(
            spdx_id="Apache-2.0",
            name="Apache License 2.0",
            reference="https://example.com/Apache-2.0",
            is_osi_approved=True,
            is_deprecated=False,
            generation=self.baseline_generation,
        )
        self.gpl_deprecated = License.objects.create(
            spdx_id="GPL-1.0",
            name="GNU General Public License v1.0",
            reference="https://example.com/GPL-1.0",
            is_osi_approved=True,
            is_deprecated=True,
            generation=self.baseline_generation,
        )

    def tearDown(self) -> None:
        patch.stopall()
        super().tearDown()

    def _payload_with_current_licenses(self) -> bytes:
        return _payload(
            [
                _spdx_entry("MIT", name="MIT license new name", reference="https://example.com/MIT-new"),
                _spdx_entry("Apache-2.0", name="Apache License 2.0", reference="https://example.com/Apache-2.0"),
                _spdx_entry("BSD-3-Clause", name="BSD 3-Clause License"),
            ]
        )

    def test_refresh_publishes_a_new_current_generation(self):
        _mock_response(self._payload_with_current_licenses())

        message = import_licenses()

        new_generation = License_Generation.objects.get(status=License_Generation.STATUS_CURRENT)
        self.assertEqual(self.baseline_generation.sequence + 1, new_generation.sequence)
        self.assertEqual(3, new_generation.license_count)
        self.assertEqual("2026-01-01", new_generation.spdx_release_date)
        self.assertIsNotNone(new_generation.published_at)

        self.baseline_generation.refresh_from_db()
        self.assertEqual(License_Generation.STATUS_SUPERSEDED, self.baseline_generation.status)

        current_ids = set(License.objects.current().values_list("spdx_id", flat=True))
        self.assertEqual({"MIT", "Apache-2.0", "BSD-3-Clause"}, current_ids)

        mit_new = License.objects.current().get(spdx_id="MIT")
        self.assertEqual("MIT license new name", mit_new.name)
        self.assertEqual("https://example.com/MIT-new", mit_new.reference)
        self.assertNotEqual(self.mit_old.pk, mit_new.pk)

        # The old generation and its rows are retained, no preparing generation exists
        self.assertEqual(
            0, License_Generation.objects.filter(status=License_Generation.STATUS_PREPARING).count()
        )
        self.assertEqual(3, License.objects.filter(generation=self.baseline_generation).count())
        self.assertIn("generation", message)

    def test_truncated_response_keeps_the_previous_generation(self):
        _mock_response(b'{"releaseDate": "2026-01-01", "licenses": [{"licenseId": "MI')

        with self.assertRaises(LicenseRefreshValidationError):
            import_licenses()

        self.assertEqual(
            self.baseline_generation.pk,
            License_Generation.objects.get(status=License_Generation.STATUS_CURRENT).pk,
        )
        self.assertEqual(
            0, License_Generation.objects.filter(status=License_Generation.STATUS_PREPARING).count()
        )
        self.assertEqual({"MIT", "Apache-2.0", "GPL-1.0"}, set(License.objects.current().values_list("spdx_id", flat=True)))

    def test_duplicate_identifiers_are_rejected(self):
        _mock_response(_payload([_spdx_entry("MIT"), _spdx_entry("MIT")]))

        with self.assertRaises(LicenseRefreshValidationError):
            import_licenses()

        self.assertEqual(
            self.baseline_generation.pk,
            License_Generation.objects.get(status=License_Generation.STATUS_CURRENT).pk,
        )

    def test_empty_license_list_is_rejected(self):
        _mock_response(_payload([]))

        with self.assertRaises(LicenseRefreshValidationError):
            import_licenses()

    def test_identical_directory_is_not_published_again(self):
        generations_before = License_Generation.objects.count()

        _mock_response(self._payload_with_current_licenses())
        import_licenses()
        self.assertEqual(generations_before + 1, License_Generation.objects.count())

        message = import_licenses()
        self.assertEqual(generations_before + 1, License_Generation.objects.count())
        self.assertIn("up to date", message)

    def test_custom_license_is_kept_and_delisted_spdx_license_is_dropped(self):
        custom_old = License.objects.create(
            spdx_id="Custom-1.0",
            name="My custom license",
            reference="https://example.com/custom",
            is_osi_approved=False,
            is_deprecated=False,
            generation=self.baseline_generation,
            is_custom=True,
        )

        # GPL-1.0 is in the old generation but no longer part of the directory
        _mock_response(self._payload_with_current_licenses())
        import_licenses()

        current = License_Generation.objects.get(status=License_Generation.STATUS_CURRENT)
        current_ids = set(License.objects.current().values_list("spdx_id", flat=True))
        self.assertIn("Custom-1.0", current_ids)
        self.assertNotIn("GPL-1.0", current_ids)

        custom_new = License.objects.current().get(spdx_id="Custom-1.0")
        self.assertTrue(custom_new.is_custom)
        self.assertEqual("My custom license", custom_new.name)
        self.assertNotEqual(custom_old.pk, custom_new.pk)

        # The custom license is carried into every future generation as well
        _mock_response(
            _payload(
                [
                    _spdx_entry("MIT", name="MIT license new name", reference="https://example.com/MIT-new"),
                    _spdx_entry("Apache-2.0"),
                    _spdx_entry("BSD-3-Clause"),
                    _spdx_entry("ISC"),
                ],
                release_date="2026-02-01",
            )
        )
        import_licenses()
        self.assertIn(
            "Custom-1.0", set(License.objects.current().values_list("spdx_id", flat=True))
        )

    def test_license_group_memberships_are_preserved(self):
        license_group = License_Group.objects.create(name="Group 1")
        license_group.licenses.add(self.mit_old, self.gpl_deprecated)

        _mock_response(self._payload_with_current_licenses())
        import_licenses()

        current_ids = set(license_group.licenses.all().values_list("spdx_id", flat=True))
        # MIT survives with a new row, GPL-1.0 left the directory and the group
        self.assertEqual({"MIT"}, current_ids)

        mit_new = License.objects.current().get(spdx_id="MIT")
        self.assertIn(mit_new, license_group.licenses.all())
        self.assertNotIn(self.mit_old, license_group.licenses.all())

    def test_license_policy_items_point_to_the_new_generation(self):
        policy = License_Policy.objects.create(name="Policy 1")
        policy_item = License_Policy_Item.objects.create(
            license_policy=policy,
            license=self.mit_old,
            evaluation_result=License_Policy_Evaluation_Result.RESULT_ALLOWED,
        )

        _mock_response(self._payload_with_current_licenses())
        import_licenses()

        policy_item.refresh_from_db()
        mit_new = License.objects.current().get(spdx_id="MIT")
        self.assertEqual(mit_new.pk, policy_item.license.pk)

    def test_license_component_foreign_keys_are_remapped(self):
        product = Product.objects.create(name="Product with license components")
        license_component = License_Component.objects.create(
            product=product,
            identity_hash="identity-hash-1",
            component_name="component",
            component_version="1.0",
            component_name_version="component:1.0",
            numerical_evaluation_result=3,
            imported_declared_spdx_license=self.apache_old,
            imported_concluded_spdx_license=self.mit_old,
            manual_concluded_spdx_license=self.mit_old,
            effective_spdx_license=self.mit_old,
        )

        _mock_response(self._payload_with_current_licenses())
        import_licenses()

        license_component.refresh_from_db()
        mit_new = License.objects.current().get(spdx_id="MIT")
        apache_new = License.objects.current().get(spdx_id="Apache-2.0")
        self.assertEqual(mit_new.pk, license_component.effective_spdx_license.pk)
        self.assertEqual(mit_new.pk, license_component.manual_concluded_spdx_license.pk)
        self.assertEqual(mit_new.pk, license_component.imported_concluded_spdx_license.pk)
        self.assertEqual(apache_new.pk, license_component.imported_declared_spdx_license.pk)

    def test_concluded_license_foreign_keys_are_remapped(self):
        product = Product.objects.create(name="Product with concluded license")
        user = User.objects.create(username="concluded-user")
        concluded_license = Concluded_License.objects.create(
            product=product,
            component_purl_type="npm",
            component_name="component",
            component_version="1.0",
            manual_concluded_spdx_license=self.mit_old,
            user=user,
        )

        _mock_response(self._payload_with_current_licenses())
        import_licenses()

        concluded_license.refresh_from_db()
        mit_new = License.objects.current().get(spdx_id="MIT")
        self.assertEqual(mit_new.pk, concluded_license.manual_concluded_spdx_license.pk)

    def test_cache_created_before_refresh_keeps_reading_the_old_generation(self):
        cache = SPDXLicenseCache()
        self.assertEqual(self.baseline_generation.pk, cache.generation_id)

        _mock_response(self._payload_with_current_licenses())
        import_licenses()

        # New license of the new generation is not visible to the pinned cache
        self.assertIsNone(cache.get("BSD-3-Clause"))
        # Existing licenses are still the rows of the old generation
        self.assertEqual(self.mit_old.pk, cache.get("MIT").pk)

        # A cache created after the refresh reads the new generation
        new_cache = SPDXLicenseCache()
        self.assertNotEqual(self.baseline_generation.pk, new_cache.generation_id)
        mit_new = License.objects.current().get(spdx_id="MIT")
        self.assertEqual(mit_new.pk, new_cache.get("MIT").pk)
        self.assertEqual(
            License.objects.current().get(spdx_id="BSD-3-Clause").pk,
            new_cache.get("BSD-3-Clause").pk,
        )

    def test_stale_preparing_generation_is_cleaned_up(self):
        stale_generation = License_Generation.objects.create(
            sequence=99,
            status=License_Generation.STATUS_PREPARING,
            license_count=1,
        )
        stale_generation.started_at = timezone.now() - timedelta(hours=1)
        stale_generation.save()
        License.objects.create(
            spdx_id="STALE",
            name="Stale candidate",
            generation=stale_generation,
        )

        removed = cleanup_stale_generations()

        self.assertEqual(1, removed)
        self.assertFalse(License_Generation.objects.filter(pk=stale_generation.pk).exists())
        self.assertEqual(0, License.objects.filter(spdx_id="STALE").count())
        # The confirmed generation is untouched
        self.assertEqual(
            self.baseline_generation.pk,
            License_Generation.objects.get(status=License_Generation.STATUS_CURRENT).pk,
        )

    def test_older_prepared_generation_cannot_overwrite_newer_current(self):
        # An older task prepared generation #2, but a concurrent task already
        # published generation #3 before #2 commits.
        older_generation = License_Generation.objects.create(
            sequence=self.baseline_generation.sequence + 1,
            status=License_Generation.STATUS_PREPARING,
            checksum="older",
            license_count=1,
        )
        License.objects.create(
            spdx_id="MIT",
            name="MIT from older candidate",
            generation=older_generation,
        )

        newer_generation = License_Generation.objects.create(
            sequence=self.baseline_generation.sequence + 2,
            status=License_Generation.STATUS_CURRENT,
            checksum="newer",
            license_count=1,
            published_at=timezone.now(),
        )
        License.objects.create(spdx_id="MIT", name="MIT from newer candidate", generation=newer_generation)
        self.baseline_generation.status = License_Generation.STATUS_SUPERSEDED
        self.baseline_generation.save()

        published = publish_prepared_generation(older_generation)

        self.assertFalse(published)
        self.assertEqual(
            newer_generation.pk,
            License_Generation.objects.get(status=License_Generation.STATUS_CURRENT).pk,
        )
        older_generation.refresh_from_db()
        self.assertEqual(License_Generation.STATUS_FAILED, older_generation.status)
        self.assertEqual(0, License.objects.filter(generation=older_generation).count())
        self.assertEqual(
            "MIT from newer candidate",
            License.objects.current().get(spdx_id="MIT").name,
        )
