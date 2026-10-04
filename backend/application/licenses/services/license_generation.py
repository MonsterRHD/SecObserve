import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from json import JSONDecodeError
from typing import Optional

import requests
from django.db import connection, transaction
from django.db.models import Max
from django.utils import timezone

from application.licenses.models import (
    Concluded_License,
    License,
    License_Component,
    License_Generation,
    License_Group,
    License_Policy_Item,
)
from application.licenses.services.spdx_license_cache import SPDX_ID_REGEX

logger = logging.getLogger("secobserve.licenses")

SPDX_LICENSES_URL = "https://raw.githubusercontent.com/spdx/license-list-data/refs/heads/main/json/licenses.json"

BATCH_SIZE = 500
# A PREPARING generation that is still present that long after its creation can
# no longer belong to a live refresh and is considered orphaned by a restart.
STALE_GENERATIONS_MIN_AGE_MINUTES = 2

# Foreign key columns pointing from referencing tables to licenses_license.
# On publishing a generation every value pointing at a license of the previous
# generation is remapped to the license with the same SPDX id in the new
# generation, so policy items, license groups and components keep working.
LICENSE_COMPONENT_FK_COLUMNS = [
    "imported_declared_spdx_license_id",
    "imported_concluded_spdx_license_id",
    "manual_concluded_spdx_license_id",
    "effective_spdx_license_id",
]


class LicenseRefreshValidationError(Exception):
    """Raised when the downloaded SPDX directory is incomplete or invalid."""


class LicenseRefreshSkipped(Exception):
    """Raised internally when a refresh must not publish a generation."""


@dataclass
class SPDXLicenseEntry:
    spdx_id: str
    name: str
    reference: str
    is_osi_approved: Optional[bool]
    is_deprecated: Optional[bool]


@dataclass
class SPDXCandidate:
    entries: list[SPDXLicenseEntry]
    checksum: str
    spdx_release_date: str


def download_and_validate_spdx_licenses() -> SPDXCandidate:
    # The complete directory is downloaded and validated before any database
    # write, so truncated responses, duplicate identifiers or malformed payloads
    # can never produce a partially visible directory.
    response = requests.get(SPDX_LICENSES_URL, timeout=60, stream=True)
    response.raise_for_status()

    try:
        data = json.loads(response.content)
    except (JSONDecodeError, UnicodeDecodeError) as e:
        raise LicenseRefreshValidationError("SPDX license list response is not valid JSON") from e

    if not isinstance(data, dict):
        raise LicenseRefreshValidationError("SPDX license list response has an unexpected structure")

    raw_licenses = data.get("licenses")
    if not isinstance(raw_licenses, list) or not raw_licenses:
        raise LicenseRefreshValidationError("SPDX license list response contains no licenses")

    entries: list[SPDXLicenseEntry] = []
    seen_spdx_ids: set[str] = set()
    for raw_license in raw_licenses:
        entry = _validate_spdx_license(raw_license)
        if entry.spdx_id in seen_spdx_ids:
            raise LicenseRefreshValidationError(
                f"SPDX license list response contains duplicate license id {entry.spdx_id}"
            )
        seen_spdx_ids.add(entry.spdx_id)
        entries.append(entry)

    release_date = data.get("releaseDate")
    if release_date is not None and not isinstance(release_date, str):
        raise LicenseRefreshValidationError("SPDX license list release date has an unexpected type")

    checksum = _calculate_checksum(entries)
    return SPDXCandidate(entries=entries, checksum=checksum, spdx_release_date=release_date or "")


def _validate_spdx_license(raw_license: object) -> SPDXLicenseEntry:
    if not isinstance(raw_license, dict):
        raise LicenseRefreshValidationError("SPDX license list contains an entry with an unexpected structure")

    spdx_id = raw_license.get("licenseId")
    if not isinstance(spdx_id, str) or not SPDX_ID_REGEX.match(spdx_id):
        raise LicenseRefreshValidationError("SPDX license list contains an entry without a valid licenseId")

    name = raw_license.get("name", "")
    reference = raw_license.get("reference", "")
    is_osi_approved = raw_license.get("isOsiApproved")
    is_deprecated = raw_license.get("isDeprecatedLicenseId")

    if not isinstance(name, str) or not isinstance(reference, str):
        raise LicenseRefreshValidationError(f"SPDX license {spdx_id} has an invalid name or reference")
    if is_osi_approved is not None and not isinstance(is_osi_approved, bool):
        raise LicenseRefreshValidationError(f"SPDX license {spdx_id} has an invalid isOsiApproved flag")
    if is_deprecated is not None and not isinstance(is_deprecated, bool):
        raise LicenseRefreshValidationError(f"SPDX license {spdx_id} has an invalid isDeprecatedLicenseId flag")

    return SPDXLicenseEntry(
        spdx_id=spdx_id,
        name=name,
        reference=reference,
        is_osi_approved=is_osi_approved,
        is_deprecated=is_deprecated,
    )


def _calculate_checksum(entries: list[SPDXLicenseEntry]) -> str:
    normalized = [
        [entry.spdx_id, entry.name, entry.reference, entry.is_osi_approved, entry.is_deprecated]
        for entry in sorted(entries, key=lambda e: e.spdx_id)
    ]
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prepare_spdx_generation(candidate: SPDXCandidate, trigger: str = "") -> License_Generation:
    # Creates the candidate generation and its license rows in one transaction.
    # The rows are invisible to all readers until the generation is published.
    with transaction.atomic():
        current_generation = _lock_current_generation()
        if current_generation is None:
            raise LicenseRefreshSkipped("Another SPDX license refresh is in progress, skipping.")

        if current_generation.license_count > 0 and current_generation.checksum == candidate.checksum:
            raise LicenseRefreshSkipped("SPDX license list is already up to date, skipping.")

        sequence = (License_Generation.objects.aggregate(max_sequence=Max("sequence"))["max_sequence"] or 0) + 1
        generation = License_Generation.objects.create(
            sequence=sequence,
            status=License_Generation.STATUS_PREPARING,
            trigger=trigger,
            license_count=_count_generation_licenses(current_generation, candidate),
            checksum=candidate.checksum,
            spdx_release_date=candidate.spdx_release_date,
        )

        _create_generation_licenses(generation, current_generation, candidate)

    return generation


def _count_generation_licenses(current_generation: License_Generation, candidate: SPDXCandidate) -> int:
    candidate_ids = {entry.spdx_id for entry in candidate.entries}
    custom_ids = set(
        License.objects.for_generation(current_generation.pk).filter(is_custom=True).values_list("spdx_id", flat=True)
    )
    # Candidate ids and custom licenses not contained in the candidate form the
    # new generation. Custom licenses contained in the candidate are counted
    # once as part of the candidate.
    return len(candidate_ids | custom_ids)


def _create_generation_licenses(
    generation: License_Generation, current_generation: License_Generation, candidate: SPDXCandidate
) -> None:
    custom_licenses = {
        license.spdx_id: license
        for license in License.objects.for_generation(current_generation.pk).filter(is_custom=True)
    }

    licenses_to_create: list[License] = []
    for entry in candidate.entries:
        # A custom license keeps its flag even if an id with the same name is
        # also part of the SPDX directory; its attributes are updated from the
        # directory like any other license.
        is_custom = custom_licenses.pop(entry.spdx_id, None) is not None
        licenses_to_create.append(
            License(
                spdx_id=entry.spdx_id,
                name=entry.name,
                reference=entry.reference,
                is_osi_approved=entry.is_osi_approved,
                is_deprecated=entry.is_deprecated,
                generation=generation,
                is_custom=is_custom,
            )
        )

    # Custom licenses that are not part of the SPDX directory are carried into
    # the new generation unchanged, so a refresh can never delete them.
    for custom_license in custom_licenses.values():
        licenses_to_create.append(
            License(
                spdx_id=custom_license.spdx_id,
                name=custom_license.name,
                reference=custom_license.reference,
                is_osi_approved=custom_license.is_osi_approved,
                is_deprecated=custom_license.is_deprecated,
                generation=generation,
                is_custom=True,
            )
        )

    for offset in range(0, len(licenses_to_create), BATCH_SIZE):
        batch_end = offset + BATCH_SIZE
        License.objects.bulk_create(licenses_to_create[offset:batch_end])


def publish_prepared_generation(generation: License_Generation) -> bool:
    # Publishes a prepared generation in a single transaction: all references to
    # licenses are remapped and the current generation pointer is switched
    # atomically. Returns False if a newer generation won the race.
    try:
        with transaction.atomic():
            current_generation = _lock_current_generation()
            if current_generation is None:
                raise LicenseRefreshSkipped("Another SPDX license refresh is in progress, skipping.")

            prepared_generation = License_Generation.objects.select_for_update().filter(pk=generation.pk).first()
            if prepared_generation is None:
                raise LicenseRefreshSkipped("Prepared SPDX license generation no longer exists, skipping.")

            if current_generation.pk != prepared_generation.pk:
                if current_generation.sequence >= prepared_generation.sequence:
                    # A newer generation has been published by a concurrent task.
                    # The older candidate must never become current.
                    raise LicenseRefreshSkipped(
                        f"Generation {prepared_generation.sequence} is older than the current generation "
                        f"{current_generation.sequence}, skipping."
                    )

            _remap_references(current_generation, prepared_generation)

            prepared_generation.status = License_Generation.STATUS_CURRENT
            prepared_generation.published_at = timezone.now()
            prepared_generation.error_message = ""
            prepared_generation.save()

            if current_generation.pk != prepared_generation.pk:
                current_generation.status = License_Generation.STATUS_SUPERSEDED
                current_generation.save(update_fields=["status"])

        return True
    except LicenseRefreshSkipped:
        _discard_generation(generation, "Superseded by a newer generation before publishing")
        return False
    except Exception as e:
        _discard_generation(generation, str(e))
        raise


def _lock_current_generation() -> Optional[License_Generation]:
    # Row lock on the current generation serializes refreshes across processes.
    # skip_locked makes a second scheduled/manual refresh fail fast instead of
    # waiting for the running refresh. On SQLite the lock is a no-op, the
    # ordering check in publish_prepared_generation still guarantees correctness.
    return (
        License_Generation.objects.select_for_update(skip_locked=True)
        .filter(status=License_Generation.STATUS_CURRENT)
        .first()
    )


def _remap_references(old_generation: License_Generation, new_generation: License_Generation) -> None:
    old_id_by_spdx = dict(License.objects.filter(generation=old_generation).values_list("spdx_id", "id"))
    new_id_by_spdx = dict(License.objects.filter(generation=new_generation).values_list("spdx_id", "id"))
    id_pairs = [
        (old_id, new_id_by_spdx[spdx_id]) for spdx_id, old_id in old_id_by_spdx.items() if spdx_id in new_id_by_spdx
    ]
    old_generation_license_ids = list(old_id_by_spdx.values())

    if id_pairs:
        _remap_foreign_key_column(
            License_Policy_Item._meta.db_table,
            "license_id",
            id_pairs,
        )
        for column in LICENSE_COMPONENT_FK_COLUMNS:
            _remap_foreign_key_column(License_Component._meta.db_table, column, id_pairs)
        _remap_foreign_key_column(
            Concluded_License._meta.db_table,
            "manual_concluded_spdx_license_id",
            id_pairs,
        )

    # Groups must only reference rows of the new current generation even if
    # the new directory shares no license with the previous generation.
    _remap_license_group_memberships(id_pairs, old_generation_license_ids)


def _remap_foreign_key_column(table: str, column: str, id_pairs: list[tuple[int, int]]) -> None:
    # One UPDATE with a CASE expression remaps all ids in the column without
    # loading the referencing rows into memory. Chunked to keep the statement
    # small for all supported databases.
    quote_name = connection.ops.quote_name
    quoted_table = quote_name(table)
    quoted_column = quote_name(column)

    for offset in range(0, len(id_pairs), BATCH_SIZE):
        chunk_end = offset + BATCH_SIZE
        chunk = id_pairs[offset:chunk_end]
        when_clauses = " ".join("WHEN %s THEN %s" for _ in chunk)
        params: list[int] = []
        for old_id, new_id in chunk:
            params.extend([old_id, new_id])
        old_ids = [old_id for old_id, _ in chunk]
        params.extend(old_ids)

        placeholders = ", ".join("%s" for _ in chunk)
        sql = (
            f"UPDATE {quoted_table} SET {quoted_column} = CASE {quoted_column} {when_clauses} "
            f"ELSE {quoted_column} END WHERE {quoted_column} IN ({placeholders})"
        )
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def _remap_license_group_memberships(id_pairs: list[tuple[int, int]], old_generation_license_ids: list[int]) -> None:
    through = License_Group.licenses.through
    old_to_new = dict(id_pairs)

    existing_memberships = list(
        through.objects.filter(license_id__in=old_generation_license_ids).values_list("license_group_id", "license_id")
    )
    # Memberships of licenses carried into the new generation are copied to the
    # new rows, so existing license group memberships survive every refresh.
    new_memberships = [
        through(license_group_id=license_group_id, license_id=old_to_new[old_license_id])
        for license_group_id, old_license_id in existing_memberships
        if old_license_id in old_to_new
    ]
    if new_memberships:
        through.objects.bulk_create(new_memberships, ignore_conflicts=True, batch_size=BATCH_SIZE)

    # Groups must only contain rows of the current generation. Memberships of
    # licenses carried over are replaced by the copied rows above; licenses that
    # are no longer part of the directory leave the groups with them. Custom
    # licenses are always carried over, so their memberships are never lost.
    through.objects.filter(license_id__in=old_generation_license_ids).delete()


def _discard_generation(generation: License_Generation, error_message: str) -> None:
    try:
        with transaction.atomic():
            # Candidate rows are never referenced from committed data because
            # the reference remap happens in the publish transaction only.
            License.objects.filter(generation=generation).delete()
            generation.status = License_Generation.STATUS_FAILED
            generation.error_message = error_message[:2048]
            generation.save(update_fields=["status", "error_message"])
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not discard failed license generation %s", generation.pk)


def cleanup_stale_generations() -> int:
    # Removes generations that never got published because the process was killed
    # during preparation or publishing, together with their candidate license
    # rows. The last confirmed (current) generation is always kept.
    if License_Generation._meta.db_table not in connection.introspection.table_names():
        return 0

    removed = 0
    cutoff = timezone.now() - timedelta(minutes=STALE_GENERATIONS_MIN_AGE_MINUTES)
    stale_generations = License_Generation.objects.filter(
        status=License_Generation.STATUS_PREPARING, started_at__lt=cutoff
    )
    for generation in stale_generations:
        try:
            with transaction.atomic():
                License.objects.filter(generation=generation).delete()
                generation.delete()
                removed += 1
                logger.warning("Removed stale (unpublished) license generation %s", generation.sequence)
        except Exception:  # pylint: disable=broad-except
            logger.exception("Could not remove stale license generation %s", generation.pk)

    # Failed generations are kept for auditing, but they must not retain
    # candidate license rows.
    for generation in License_Generation.objects.filter(status=License_Generation.STATUS_FAILED):
        License.objects.filter(generation=generation).delete()

    return removed
