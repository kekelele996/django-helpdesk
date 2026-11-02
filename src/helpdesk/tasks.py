import logging

from celery import shared_task

from .digest import STATE_FAILED, send_all_digests
from .email import process_email

logger = logging.getLogger("helpdesk")


@shared_task
def helpdesk_process_email():
    process_email()


@shared_task
def helpdesk_send_saved_search_digests():
    """Send the daily digest of every enabled saved search.

    Scheduled independently (e.g. via django-celery-beat). Returns the number
    of failed digests so a scheduler/monitor can alert on a non-zero result.
    """
    results = send_all_digests()
    failures = [result for result in results if result.state == STATE_FAILED]
    if failures:
        logger.error(
            "%s saved search digest(s) failed: %s",
            len(failures),
            ", ".join(str(result.saved_search_id) for result in failures),
        )
    return len(failures)
