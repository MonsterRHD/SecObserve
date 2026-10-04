from dataclasses import replace
from datetime import datetime, timezone

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from application.vex.services.vex_candidate import (
    ParsedVEXDocument,
    StatementData,
    calculate_content_hash,
    compare_revisions,
    parse_release_date,
)
from application.vex.types import VEX_Document_Type, VEX_Status


def _candidate(
    version: str = "1",
    current: str = "2024-01-01T00:00:00+00:00",
    initial: str = "2024-01-01T00:00:00+00:00",
    statements: list[StatementData] | None = None,
    has_source_release_dates: bool = True,
) -> ParsedVEXDocument:
    current_date = datetime.fromisoformat(current)
    initial_date = datetime.fromisoformat(initial)
    return ParsedVEXDocument(
        type=VEX_Document_Type.VEX_DOCUMENT_TYPE_OPENVEX,
        document_id="doc-1",
        author="author-1",
        role="vendor",
        version=version,
        initial_release_date=initial_date,
        current_release_date=current_date,
        statements=statements or [],
        product_purls=set(),
        has_source_release_dates=has_source_release_dates,
    )


class TestRevisionComparison(SimpleTestCase):
    def test_numeric_versions(self):
        self.assertEqual("newer", compare_revisions(_candidate("2"), "1"))
        self.assertEqual("older", compare_revisions(_candidate("1"), "2"))
        self.assertEqual("newer", compare_revisions(_candidate("10"), "2"))
        self.assertEqual("equal", compare_revisions(_candidate("1"), "1"))

    def test_dotted_numeric_versions(self):
        self.assertEqual("newer", compare_revisions(_candidate("1.0.1"), "1.0"))
        self.assertEqual("older", compare_revisions(_candidate("1.0"), "1.0.1"))

    def test_equal_version_is_same_revision_even_with_other_release_dates(self):
        # Same version must never be treated as newer/older based on dates;
        # the content hash decides whether the replay is idempotent or a conflict.
        self.assertEqual(
            "equal",
            compare_revisions(_candidate("1", current="2024-02-01T00:00:00+00:00"), "1"),
        )

    def test_non_numeric_versions_fall_back_to_strings(self):
        self.assertEqual(
            "older",
            compare_revisions(_candidate("rev-a"), "rev-b"),
        )


class TestContentHash(SimpleTestCase):
    def _statement(self, vulnerability_id: str = "CVE-1") -> StatementData:
        return StatementData(
            vulnerability_id=vulnerability_id,
            description="description",
            status=VEX_Status.VEX_STATUS_NOT_AFFECTED,
            justification="",
            impact="impact",
            remediation="",
            product_purl="pkg:pypi/product@1.0",
            component_purl="pkg:pypi/component@1.0",
        )

    def test_statement_order_does_not_change_hash(self):
        candidate_1 = _candidate(statements=[self._statement("CVE-1"), self._statement("CVE-2")])
        candidate_2 = _candidate(statements=[self._statement("CVE-2"), self._statement("CVE-1")])
        self.assertEqual(calculate_content_hash(candidate_1), calculate_content_hash(candidate_2))

    def test_timezone_representation_equivalent(self):
        candidate_zulu = _candidate(
            current="2024-01-01T00:00:00+00:00",
            initial="2024-01-01T00:00:00+00:00",
        )
        candidate_offset = _candidate(
            current="2024-01-01T02:00:00+02:00",
            initial="2024-01-01T02:00:00+02:00",
        )
        self.assertEqual(calculate_content_hash(candidate_zulu), calculate_content_hash(candidate_offset))

    def test_statement_field_changes_hash(self):
        baseline = _candidate(statements=[self._statement()])
        changed = replace(
            self._statement(),
            status=VEX_Status.VEX_STATUS_AFFECTED,
            impact="",
            remediation="Upgrade",
        )
        candidate_changed = _candidate(statements=[changed])
        self.assertNotEqual(calculate_content_hash(baseline), calculate_content_hash(candidate_changed))

    def test_release_date_change_changes_hash(self):
        baseline = _candidate(current="2024-01-01T00:00:00+00:00")
        changed = _candidate(current="2024-02-01T00:00:00+00:00")
        self.assertNotEqual(calculate_content_hash(baseline), calculate_content_hash(changed))

    def test_server_generated_dates_are_excluded_from_hash(self):
        baseline = _candidate(current="2024-01-01T00:00:00+00:00", has_source_release_dates=False)
        changed = _candidate(current="2024-02-01T00:00:00+00:00", has_source_release_dates=False)
        self.assertEqual(calculate_content_hash(baseline), calculate_content_hash(changed))


class TestParseReleaseDate(SimpleTestCase):
    def test_valid_datetime(self):
        parsed = parse_release_date("2024-01-01T00:00:00Z", "timestamp")
        self.assertEqual(datetime(2024, 1, 1, tzinfo=timezone.utc), parsed)

    def test_date_only_is_accepted(self):
        parsed = parse_release_date("2024-01-01", "timestamp")
        self.assertEqual(datetime(2024, 1, 1), parsed)

    def test_missing_value_raises(self):
        with self.assertRaises(ValidationError):
            parse_release_date("", "timestamp")

    def test_invalid_value_raises(self):
        with self.assertRaises(ValidationError):
            parse_release_date("not-a-date", "timestamp")
