from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "application.issue_tracker"
    verbose_name = "Issue Tracker"

    def ready(self) -> None:
        import config.schema  # noqa: F401 pylint: disable=import-outside-toplevel, unused-import
