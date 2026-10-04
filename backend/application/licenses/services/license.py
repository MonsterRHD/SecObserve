import logging

from application.licenses.models import License, License_Generation
from application.licenses.services.license_generation import (
    LicenseRefreshSkipped,
    cleanup_stale_generations,
    download_and_validate_spdx_licenses,
    prepare_spdx_generation,
    publish_prepared_generation,
)

logger = logging.getLogger("secobserve.licenses")


def import_licenses(trigger: str = License_Generation.TRIGGER_REFRESH) -> str:
    # The SPDX directory is downloaded and fully validated as a candidate first.
    # Only after that it is prepared and published as an identifiable
    # generation; until publication all readers keep seeing the previous
    # complete generation.

    # A process may have been killed during a previous refresh. Remove
    # unpublished generations left behind before preparing a new one.
    try:
        cleanup_stale_generations()
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not clean up stale license generations before the refresh")

    candidate = download_and_validate_spdx_licenses()

    try:
        generation = prepare_spdx_generation(candidate, trigger)
    except LicenseRefreshSkipped as e:
        return str(e)

    published = publish_prepared_generation(generation)
    if not published:
        return f"Generation {generation.sequence} was not published, the current generation stays in use."

    return _build_result_message(generation)


def _build_result_message(generation: License_Generation) -> str:
    current_license_ids = set(License.objects.current().values_list("spdx_id", flat=True))
    previous_generation = (
        License_Generation.objects.filter(license_count__gt=0)
        .exclude(pk=generation.pk)
        .order_by("-sequence")
        .first()
    )
    previous_license_ids: set[str] = set()
    changed_licenses = 0
    if previous_generation:
        previous_licenses = {
            license.spdx_id: license
            for license in License.objects.filter(generation=previous_generation)
        }
        previous_license_ids = set(previous_licenses.keys())
        new_licenses = {license.spdx_id: license for license in License.objects.filter(generation=generation)}
        for spdx_id in current_license_ids & previous_license_ids:
            new_license = new_licenses[spdx_id]
            old_license = previous_licenses[spdx_id]
            if (
                new_license.name != old_license.name
                or new_license.reference != old_license.reference
                or new_license.is_osi_approved != old_license.is_osi_approved
                or new_license.is_deprecated != old_license.is_deprecated
            ):
                changed_licenses += 1

    created = len(current_license_ids - previous_license_ids)
    return (
        f"SPDX license generation {generation.sequence} published "
        f"(release {generation.spdx_release_date or 'n/a'}): "
        f"licenses created: {created}, licenses updated: {changed_licenses}, "
        f"licenses in generation: {generation.license_count}"
    )
