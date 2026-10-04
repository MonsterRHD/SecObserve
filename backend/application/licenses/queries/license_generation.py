from typing import Optional

from application.licenses.models import License_Generation


def get_current_generation() -> Optional[License_Generation]:
    try:
        return License_Generation.objects.get(status=License_Generation.STATUS_CURRENT)
    except License_Generation.DoesNotExist:
        return None


def get_current_generation_pk() -> Optional[int]:
    current_generation = get_current_generation()
    return current_generation.pk if current_generation else None
