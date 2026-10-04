from typing import Optional

from application.licenses.models import License


def get_license(license_id: int) -> Optional[License]:
    try:
        return License.objects.get(id=license_id)
    except License.DoesNotExist:
        return None


def get_license_by_spdx_id(spdx_id: str, generation_id: Optional[int] = None) -> Optional[License]:
    # License lookups are scoped to a single generation. Without an explicit
    # generation the current generation is used. A generation id captured at the
    # start of a product import keeps the whole import on one generation even if
    # a newer generation is published while the import is running.
    licenses = License.objects.all()
    if generation_id is None:
        licenses = licenses.current()
    else:
        licenses = licenses.for_generation(generation_id)

    try:
        return licenses.get(spdx_id=spdx_id)
    except License.DoesNotExist:
        return None
