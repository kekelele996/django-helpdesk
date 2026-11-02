"""
django-helpdesk - A Django powered ticket tracker for small enterprise.

send_saved_search_digests.py - E-mail the daily digest of every saved search
                               with its digest enabled. Designed for Cron or a
                               scheduler (e.g. django-celery-beat).

The command is idempotent: each saved search remembers the point in time its
last successful digest covered, so retries after a failed send and
overlapping schedule windows never deliver the same change twice. Empty
digests are skipped silently.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand

from helpdesk import digest as digest_module
from helpdesk.digest import (
    DEFAULT_LOOKBACK,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_SENT,
    STATE_WOULD_SEND,
)
from helpdesk.models import SavedSearch


class Command(BaseCommand):
    help = (
        "Send the daily digest e-mail for saved searches with "
        "'digest enabled'. New tickets, status changes and reopenings since "
        "the last successful digest are included; empty digests are skipped."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--id",
            nargs="*",
            type=int,
            dest="ids",
            help="Only process these saved search ids (default: all enabled).",
        )
        parser.add_argument(
            "--lookback-hours",
            type=float,
            default=DEFAULT_LOOKBACK.total_seconds() / 3600,
            help=(
                "Hours covered by the very first digest of a search that has "
                "never run before (default: %(default)s)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Collect and render digests without sending or moving the "
            "watermark; reports what would happen.",
        )

    def handle(self, *args, **options):
        lookback = timedelta(hours=options["lookback_hours"])
        results = digest_module.send_all_digests(
            lookback=lookback,
            ids=options["ids"],
            dry_run=options["dry_run"],
        )

        missing_ids = set()
        if options["ids"]:
            # An unknown id is an operator error (typically a typo) worth a
            # non-zero exit instead of a silent no-op. An existing but
            # disabled search is simply reported, not an error.
            existing = set(
                SavedSearch.objects.filter(pk__in=options["ids"]).values_list(
                    "pk", flat=True
                )
            )
            missing_ids = set(options["ids"]) - existing
            if missing_ids:
                self.stderr.write(
                    self.style.ERROR(
                        "Saved search id(s) not found: "
                        f"{', '.join(map(str, sorted(missing_ids)))}"
                    )
                )
            disabled = (
                set(options["ids"])
                - missing_ids
                - set(
                    SavedSearch.objects.filter(
                        pk__in=options["ids"], digest_enabled=True
                    ).values_list("pk", flat=True)
                )
            )
            for pk in sorted(disabled):
                self.stdout.write(
                    f"Saved search {pk} has its daily digest disabled, skipped"
                )

        for result in results:
            if result.state == STATE_SENT:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Sent digest for saved search {result.saved_search_id}: "
                        f"{result.totals.get('new', 0)} new, "
                        f"{result.totals.get('reopened', 0)} reopened, "
                        f"{result.totals.get('status', 0)} status changes"
                    )
                )
            elif result.state == STATE_WOULD_SEND:
                self.stdout.write(
                    f"Would send digest for saved search {result.saved_search_id} "
                    f"(dry-run): {result.totals}"
                )
            elif result.state == STATE_EMPTY:
                self.stdout.write(
                    f"No changes for saved search {result.saved_search_id}, no e-mail sent"
                )
            elif result.state == STATE_FAILED:
                self.stderr.write(
                    self.style.ERROR(
                        f"FAILED saved search {result.saved_search_id}: {result.error}"
                    )
                )

        failed = any(result.state == STATE_FAILED for result in results)
        if failed or missing_ids:
            raise SystemExit(1)
