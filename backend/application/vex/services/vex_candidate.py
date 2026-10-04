"""In-memory VEX document candidates, revision ordering and content hashing.

The extraction parsers build a :class:`ParsedVEXDocument` without any database
access. The publisher then decides whether the candidate is older than, equal
to or newer than the already published revision and calculates a stable
content hash for idempotency/conflict detection.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError

RevisionRelation = Literal["older", "equal", "newer"]


@dataclass(frozen=True)
class StatementData:
    """Semantic content of one VEX statement, matching the VEX_Statement model."""

    vulnerability_id: str
    description: str = ""
    status: str = ""
    justification: str = ""
    impact: str = ""
    remediation: str = ""
    product_purl: str = ""
    component_purl: str = ""
    component_cyclonedx_bom_link: str = ""

    def as_tuple(self) -> tuple[str, str, str, str, str, str, str, str, str]:
        return (
            self.vulnerability_id,
            self.description,
            self.status,
            self.justification,
            self.impact,
            self.remediation,
            self.product_purl,
            self.component_purl,
            self.component_cyclonedx_bom_link,
        )


@dataclass
class ParsedVEXDocument:
    """A fully validated VEX document candidate that is not persisted yet."""

    type: str
    document_id: str
    author: str
    role: str
    version: str
    initial_release_date: datetime
    current_release_date: datetime
    statements: list[StatementData] = field(default_factory=list)
    product_purls: set[str] = field(default_factory=set)
    # False when the release dates are server side defaults instead of values
    # taken from the source document (e.g. CycloneDX without metadata/timestamp),
    # in which case they are excluded from the content hash.
    has_source_release_dates: bool = True


def parse_release_date(value: Optional[str], field_path: str) -> datetime:
    """Parse an ISO 8601 date(time) the same way Django's DateTimeField does."""
    if not value:
        raise ValidationError(f"{field_path} is missing")

    parsed = parse_datetime(value)
    if parsed is None:
        parsed_date = parse_date(value)
        if parsed_date is not None:
            parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day)
    if parsed is None:
        raise ValidationError(f"{field_path} is not a valid date: {value}")

    return parsed


def normalize_release_date(value: Optional[datetime]) -> str:
    """Normalize a datetime for hashing so equivalent timezone representations match."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def calculate_content_hash(candidate: ParsedVEXDocument) -> str:
    """Calculate a stable SHA-256 over the candidate's semantic content.

    JSON formatting/key order of the uploaded file and the order of the
    statements do not influence the hash. All nine statement fields, the
    document metadata and the normalized release dates are part of the hash.
    """
    if candidate.has_source_release_dates:
        initial_release_date = normalize_release_date(candidate.initial_release_date)
        current_release_date = normalize_release_date(candidate.current_release_date)
    else:
        initial_release_date = ""
        current_release_date = ""

    payload = {
        "type": candidate.type,
        "document_id": candidate.document_id,
        "author": candidate.author,
        "role": candidate.role,
        "version": candidate.version,
        "initial_release_date": initial_release_date,
        "current_release_date": current_release_date,
        "statements": sorted(statement.as_tuple() for statement in candidate.statements),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compare_revisions(candidate: ParsedVEXDocument, existing_version: str) -> RevisionRelation:
    """Compare a candidate against the published revision.

    Ordering rules:
    1. Numeric/dotted-numeric versions are compared numerically, else lexically.
    2. Equal versions always denote the same revision; any content or release
       date difference at the same version is handled as an idempotent replay
       or a conflict via the content hash, never as a newer/older revision.
    """
    version_comparison = _compare_versions(candidate.version, existing_version)
    if version_comparison > 0:
        return "newer"
    if version_comparison < 0:
        return "older"
    return "equal"


def _compare_versions(candidate_version: str, existing_version: str) -> int:
    candidate_number = _as_integer(candidate_version)
    existing_number = _as_integer(existing_version)
    if candidate_number is not None and existing_number is not None:
        return (candidate_number > existing_number) - (candidate_number < existing_number)

    candidate_tuple = _as_number_tuple(candidate_version)
    existing_tuple = _as_number_tuple(existing_version)
    if candidate_tuple is not None and existing_tuple is not None:
        return _compare_number_tuples(candidate_tuple, existing_tuple)

    return (candidate_version > existing_version) - (candidate_version < existing_version)


def _as_integer(value: str) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_number_tuple(value: str) -> Optional[tuple[int, ...]]:
    parts = str(value).split(".")
    numbers = []
    for part in parts:
        number = _as_integer(part)
        if number is None:
            return None
        numbers.append(number)
    return tuple(numbers)


def _compare_number_tuples(candidate: tuple[int, ...], existing: tuple[int, ...]) -> int:
    max_length = max(len(candidate), len(existing))
    for index in range(max_length):
        candidate_number = candidate[index] if index < len(candidate) else 0
        existing_number = existing[index] if index < len(existing) else 0
        if candidate_number != existing_number:
            return (candidate_number > existing_number) - (candidate_number < existing_number)
    return 0
