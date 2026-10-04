import logging

from django.apps import AppConfig
from django.db.models.signals import post_migrate

logger = logging.getLogger("secobserve.licenses")


def _cleanup_stale_generations_after_migrate(sender, **kwargs) -> None:  # pylint: disable=unused-argument
    try:
        from application.licenses.services.license_generation import (  # pylint: disable=import-outside-toplevel
            cleanup_stale_generations,
        )

        cleanup_stale_generations()
    except Exception as e:  # pylint: disable=broad-except
        logger.debug("Could not clean up stale license generations: %s", e)


class LicenseConfig(AppConfig):
    name = "application.licenses"
    verbose_name = "Licenses"

    def ready(self) -> None:
        try:
            import application.licenses.signals  # noqa F401 pylint: disable=import-outside-toplevel, unused-import
        except ImportError:
            pass

        import config.schema  # noqa: F401 pylint: disable=import-outside-toplevel, unused-import

        # Remove generations that never got published because the process was
        # killed while they were being prepared or committed. This runs after
        # database migrations (i.e. on every deployment/restart that applies
        # migrations); in addition the refresh task cleans stale generations
        # itself before preparing a new one, so a worker that restarts without
        # applying migrations self-heals on its first refresh as well.
        post_migrate.connect(
            _cleanup_stale_generations_after_migrate,
            sender=self,
            dispatch_uid="licenses.cleanup_stale_generations",
        )
