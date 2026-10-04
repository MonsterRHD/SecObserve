from huey import crontab
from huey.contrib.djhuey import db_periodic_task

from application.background_tasks.services.task_base import so_periodic_task
from application.issue_tracker.services.issue_sync_dispatcher import dispatch_pending


@db_periodic_task(crontab(minute="*/3"))
@so_periodic_task("Synchronize issue tracker issues")
def task_synchronize_issue_tracker_issues() -> str:
    return dispatch_pending()
