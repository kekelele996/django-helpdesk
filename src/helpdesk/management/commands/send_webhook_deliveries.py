"""
Sweep durable webhook deliveries that are due and (re)send them.

Designed to be run from cron or Celery beat every minute or so. Pending rows
whose retry interval has elapsed are delivered; rows that succeeded are never
touched, so after an outage of the receiving service only missed events are
replayed. Quarantined rows are skipped unless --retry-quarantined is given.
"""

from django.core.management.base import BaseCommand

from helpdesk import settings as helpdesk_settings
from helpdesk.webhooks import send_due_deliveries


class Command(BaseCommand):
    help = "Deliver due pending webhook deliveries, retrying previous failures."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=helpdesk_settings.HELPDESK_WEBHOOK_BATCH_SIZE,
            help="Maximum number of deliveries to process in one run.",
        )
        parser.add_argument(
            "--retry-quarantined",
            action="store_true",
            default=False,
            help="Also replay deliveries previously isolated after repeated failures.",
        )

    def handle(self, *args, **options):
        summary = send_due_deliveries(
            limit=options["batch_size"],
            include_quarantined=options["retry_quarantined"],
        )
        self.stdout.write(
            "Processed {processed} webhook delivery/deliveries: "
            "{succeeded} succeeded, {failed} failed, "
            "{quarantined} quarantined.".format(**summary)
        )
