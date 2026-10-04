from importlib import import_module

from django.apps import apps
from django.db import migrations
from django.test import TestCase

from application.licenses.models import (
    Concluded_License,
    License,
    License_Component,
    License_Generation,
    License_Group,
    License_Policy_Item,
)

migration = import_module("application.licenses.migrations.0025_license_generation")


class TestLicenseGenerationBaseline(TestCase):
    def setUp(self) -> None:
        # Fixture data of other test classes may be visible in this suite;
        # licenses are protected against deletion, so remove everything that
        # references them before starting from an empty directory.
        License_Group.licenses.through.objects.all().delete()
        License_Policy_Item.objects.all().delete()
        License_Component.objects.all().delete()
        Concluded_License.objects.all().delete()
        License.objects.all().delete()
        License_Generation.objects.all().delete()

    def test_baseline_generation_is_created_as_current(self):
        # Unit tests run with migrations disabled on an empty database, which
        # corresponds to a fresh installation before the first SPDX import.
        self.assertFalse(License_Generation.objects.exists())

        migration.create_baseline_generation(apps, None)

        generation = License_Generation.objects.get()
        self.assertEqual(1, generation.sequence)
        self.assertEqual(License_Generation.STATUS_CURRENT, generation.status)
        self.assertEqual(0, generation.license_count)
        self.assertIsNotNone(generation.published_at)

    def test_reverse_code_is_noop(self):
        # The migration is not reversible data-wise, reversing must be a noop.
        run_python_operations = [
            operation
            for operation in migration.Migration.operations
            if isinstance(operation, migrations.RunPython)
        ]
        self.assertEqual(1, len(run_python_operations))
        self.assertEqual(migrations.RunPython.noop, run_python_operations[0].reverse_code)
