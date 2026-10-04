from datetime import timedelta

# A lease older than this is considered orphaned by a crashed worker and can be claimed.
LOCK_TIMEOUT = timedelta(minutes=10)


def get_correlation_id(observation_id: int) -> str:
    return f"secobserveobservation{observation_id}"
