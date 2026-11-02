"""
Daily digests for saved searches.

A digest e-mails the owner of a :class:`~helpdesk.models.SavedSearch` the
tickets that are new, changed status or were reopened inside that filter since
the previous digest.

Delivery is built around two pieces of state on the saved search:

* ``digest_last_success_at`` -- a time watermark. Everything up to this point
  has already been covered. It only moves forward *after* the mail backend
  accepts the message, so a failed send retries the very same window.
* :class:`~helpdesk.models.SavedSearchDigestEvent` -- an append-only log of
  the individual events that were already delivered. Its unique key makes
  overlapping windows idempotent: the same change can never show up twice,
  even if the watermark is wound back or two windows otherwise overlap.

Empty windows still move the watermark forward but never send an e-mail.

The digest deliberately reuses :class:`helpdesk.query.__Query__` unchanged:
it always describes the very same tickets the supervisor sees when opening
the filter manually.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from smtplib import SMTPException

from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _

from helpdesk.models import (
    FollowUp,
    SavedSearch,
    SavedSearchDigestEvent,
    Ticket,
)
from helpdesk.query import get_query_class, query_from_base64
from helpdesk.user import HelpdeskUser

logger = logging.getLogger("helpdesk")

#: Window used for the very first digest of a search (no watermark yet), so
#: the first run does not dump the whole ticket history into one e-mail.
DEFAULT_LOOKBACK = timedelta(hours=24)


class DigestError(Exception):
    """Base class for digest failures."""


class DigestQueryError(DigestError):
    """The saved filter could not be decoded or executed."""


class DigestSendError(DigestError):
    """The digest e-mail could not be handed over to the mail backend."""


#: send_digest_for_search() finished without delivering an e-mail because the
#: window contained no new activity.
STATE_EMPTY = "empty"
#: An e-mail was built and accepted by the mail backend.
STATE_SENT = "sent"
#: Building or sending failed; nothing was recorded and the run can be retried.
STATE_FAILED = "failed"
#: dry-run: an e-mail would have been sent, but nothing was written.
STATE_WOULD_SEND = "would_send"


@dataclass
class Digest:
    """The ticket events collected for one digest window."""

    window_start: datetime
    window_end: datetime
    new_tickets: list = field(default_factory=list)
    reopened_followups: list = field(default_factory=list)
    status_followups: list = field(default_factory=list)

    @property
    def is_empty(self):
        return not (
            self.new_tickets or self.reopened_followups or self.status_followups
        )

    @property
    def totals(self):
        return {
            "new": len(self.new_tickets),
            "reopened": len(self.reopened_followups),
            "status": len(self.status_followups),
        }

    def event_rows(self, saved_search):
        """SavedSearchDigestEvent rows representing everything in this digest."""
        rows = [
            SavedSearchDigestEvent(
                saved_search=saved_search,
                ticket=ticket,
                followup=None,
                event_type=SavedSearchDigestEvent.EVENT_NEW,
            )
            for ticket in self.new_tickets
        ]
        rows += [
            SavedSearchDigestEvent(
                saved_search=saved_search,
                ticket=followup.ticket,
                followup=followup,
                event_type=SavedSearchDigestEvent.EVENT_REOPENED,
            )
            for followup in self.reopened_followups
        ]
        rows += [
            SavedSearchDigestEvent(
                saved_search=saved_search,
                ticket=followup.ticket,
                followup=followup,
                event_type=SavedSearchDigestEvent.EVENT_STATUS,
            )
            for followup in self.status_followups
        ]
        return rows


@dataclass
class DigestResult:
    saved_search_id: int
    state: str
    totals: dict = field(default_factory=dict)
    error: str = None


def _decode_query_params(saved_search):
    """Decode the stored filter, accepting the legacy ``b'...'`` repr format."""
    raw = saved_search.query or ""
    if raw.startswith(("b'", 'b"')):
        raw = raw[2:-1]
    return query_from_base64(raw)


def _matching_ticket_queryset(saved_search):
    """Tickets the saved filter currently matches.

    Reuses the exact same query machinery as the manual ticket list, instead
    of re-implementing filter parsing, so filter behaviour never diverges.
    """
    params = _decode_query_params(saved_search)
    query = get_query_class()(HelpdeskUser(saved_search.user), query_params=params)
    return query.get()


def collect_digest_events(
    saved_search, *, now=None, window_start=None, lookback=DEFAULT_LOOKBACK
):
    """Collect new / reopened / status-change events for one digest window.

    ``window_start`` defaults to the saved search's watermark, or to
    ``now - lookback`` when no digest has ever run. Events the search has
    already delivered in an earlier (possibly overlapping) digest are
    filtered out via :class:`SavedSearchDigestEvent`.
    """
    now = now or timezone.now()
    if window_start is None:
        window_start = saved_search.digest_last_success_at or (now - lookback)

    matching = _matching_ticket_queryset(saved_search)

    new_tickets = list(
        matching.filter(created__gt=window_start, created__lte=now).order_by(
            "created", "id"
        )
    )
    new_ticket_ids = {ticket.pk for ticket in new_tickets}

    followup_qs = FollowUp.objects.filter(
        ticket__in=matching,
        date__gt=window_start,
        date__lte=now,
        new_status__isnull=False,
    ).select_related("ticket")
    if new_ticket_ids:
        # A brand-new ticket surfaces under "new"; its same-window follow-ups
        # must not list it a second time as a status change.
        followup_qs = followup_qs.exclude(ticket__in=new_ticket_ids)

    reopened_followups = []
    status_followups = []
    for followup in followup_qs.order_by("date", "id"):
        if followup.new_status == Ticket.REOPENED_STATUS:
            reopened_followups.append(followup)
        else:
            status_followups.append(followup)

    # Idempotency backstop: drop anything an earlier digest already covered.
    delivered = set(
        SavedSearchDigestEvent.objects.filter(saved_search=saved_search).values_list(
            "ticket_id", "followup_id", "event_type"
        )
    )
    new_tickets = [
        ticket
        for ticket in new_tickets
        if (ticket.pk, None, SavedSearchDigestEvent.EVENT_NEW) not in delivered
    ]
    reopened_followups = [
        followup
        for followup in reopened_followups
        if (
            followup.ticket_id,
            followup.pk,
            SavedSearchDigestEvent.EVENT_REOPENED,
        )
        not in delivered
    ]
    status_followups = [
        followup
        for followup in status_followups
        if (
            followup.ticket_id,
            followup.pk,
            SavedSearchDigestEvent.EVENT_STATUS,
        )
        not in delivered
    ]

    return Digest(
        window_start=window_start,
        window_end=now,
        new_tickets=new_tickets,
        reopened_followups=reopened_followups,
        status_followups=status_followups,
    )


def _build_email(saved_search, digest):
    """Render the digest message; raise DigestSendError if it has nowhere to go."""
    recipient = saved_search.user.email
    if not recipient:
        raise DigestSendError(
            _(
                "Saved search %(id)s owner %(username)s has no e-mail address, "
                "so the digest cannot be sent."
            )
            % {"id": saved_search.pk, "username": saved_search.user.get_username()}
        )

    context = {
        "saved_search": saved_search,
        "window_start": digest.window_start,
        "window_end": digest.window_end,
        "new_tickets": digest.new_tickets,
        "reopened_followups": digest.reopened_followups,
        "status_followups": digest.status_followups,
    }
    subject = render_to_string(
        "helpdesk/saved_search_digest_subject.txt", context
    ).strip()
    text_body = render_to_string("helpdesk/saved_search_digest.txt", context)
    html_body = render_to_string("helpdesk/saved_search_digest.html", context)

    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        to=[recipient],
    )
    message.attach_alternative(html_body, "text/html")
    return message


def send_digest_for_search(
    saved_search, *, now=None, lookback=DEFAULT_LOOKBACK, dry_run=False
):
    """Build and deliver the digest for one saved search.

    The watermark and the delivered-event log are written in the same
    transaction, only after the mail backend accepts the message. A failure
    therefore leaves no bookkeeping behind and a later retry delivers the
    same window exactly once.

    Returns :class:`DigestResult`. Raises :class:`DigestError` subclasses on
    failure. Empty windows update the watermark but send no e-mail.
    """
    now = now or timezone.now()

    with transaction.atomic():
        # Serialize concurrent scheduler invocations for the same search.
        # SQLite ignores FOR UPDATE, but the unique event key backstops there.
        locked = (
            SavedSearch.objects.select_related("user")
            .select_for_update()
            .get(pk=saved_search.pk)
        )

        try:
            digest = collect_digest_events(locked, now=now, lookback=lookback)
        except DigestError:
            raise
        except Exception as exc:
            raise DigestQueryError(
                _(
                    "Saved search %(id)s could not be evaluated, digest skipped: "
                    "%(error)s"
                )
                % {"id": locked.pk, "error": exc}
            ) from exc

        totals = digest.totals

        if digest.is_empty:
            # Covered through 'now' without bothering anyone.
            locked.digest_last_success_at = digest.window_end
            locked.save(update_fields=["digest_last_success_at"])
            return DigestResult(locked.pk, STATE_EMPTY, totals)

        if dry_run:
            return DigestResult(locked.pk, STATE_WOULD_SEND, totals)

        message = _build_email(locked, digest)
        try:
            message.send()
        except (SMTPException, OSError) as exc:
            # Rollback: neither the watermark nor the event log moves, so the
            # retry resends exactly this window rather than skipping it.
            raise DigestSendError(
                _("Digest e-mail for saved search %(id)s failed: %(error)s")
                % {"id": locked.pk, "error": exc}
            ) from exc

        SavedSearchDigestEvent.objects.bulk_create(digest.event_rows(locked))
        locked.digest_last_success_at = digest.window_end
        locked.save(update_fields=["digest_last_success_at"])

    return DigestResult(locked.pk, STATE_SENT, totals)


def send_all_digests(*, now=None, lookback=DEFAULT_LOOKBACK, ids=None, dry_run=False):
    """Deliver digests for every enabled saved search.

    One bad search (broken query, bad recipient, SMTP failure) never blocks
    the others. Failures are returned as ``STATE_FAILED`` results instead of
    being swallowed, so callers (cron/celery) can signal them loudly.
    """
    now = now or timezone.now()
    searches = (
        SavedSearch.objects.filter(digest_enabled=True)
        .select_related("user")
        .order_by("id")
    )
    if ids:
        searches = searches.filter(pk__in=ids)

    results = []
    for saved_search in searches:
        try:
            result = send_digest_for_search(
                saved_search, now=now, lookback=lookback, dry_run=dry_run
            )
        except DigestError as exc:
            logger.exception(
                "Saved search digest failed for saved search %s", saved_search.pk
            )
            result = DigestResult(saved_search.pk, STATE_FAILED, error=str(exc))
        results.append(result)
    return results
