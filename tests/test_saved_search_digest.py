from datetime import datetime, timezone as dt_timezone
from smtplib import SMTPException
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from freezegun import freeze_time

from helpdesk.digest import (
    DigestSendError,
    STATE_EMPTY,
    STATE_FAILED,
    STATE_SENT,
    STATE_WOULD_SEND,
    send_all_digests,
    send_digest_for_search,
)
from helpdesk.models import (
    FollowUp,
    Queue,
    SavedSearch,
    SavedSearchDigestEvent,
    Ticket,
)
from helpdesk.query import query_to_base64

from .helpers import create_ticket, get_user

RUN_AT = "2026-10-20 12:00:00"
RUN_AT_DT = datetime(2026, 10, 20, 12, 0, tzinfo=dt_timezone.utc)
RETRY_AT_DT = datetime(2026, 10, 20, 13, 0, tzinfo=dt_timezone.utc)


class DigestTestCase(TestCase):
    def setUp(self):
        self.user = get_user(is_staff=True)
        self.queue = Queue.objects.create(title="Q1", slug="q1")
        self.other_queue = Queue.objects.create(title="Q2", slug="q2")
        self.search = SavedSearch.objects.create(
            user=self.user,
            title="Open in Q1",
            query=query_to_base64({"filtering": {"queue__id__in": [self.queue.pk]}}),
            digest_enabled=True,
        )

    def status_followup(self, ticket, new_status, when, title=None):
        with freeze_time(when):
            return FollowUp.objects.create(
                ticket=ticket,
                title=title or f"status -> {new_status}",
                public=True,
                new_status=new_status,
            )

    def test_first_digest_groups_new_reopened_and_status_changes(self):
        # Old ticket (created outside the 24h first-run window) gets resolved
        # and reopened inside the window.
        with freeze_time("2026-10-19 06:00:00"):
            old_ticket = create_ticket(queue=self.queue, title="old one")
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="brand new")
        self.status_followup(old_ticket, Ticket.RESOLVED_STATUS, "2026-10-20 10:00:00")
        self.status_followup(old_ticket, Ticket.REOPENED_STATUS, "2026-10-20 11:00:00")
        # Ticket outside the saved filter must never show up.
        with freeze_time("2026-10-20 08:30:00"):
            create_ticket(queue=self.other_queue, title="other queue ticket")

        with freeze_time(RUN_AT):
            result = send_digest_for_search(self.search)

        self.assertEqual(result.state, STATE_SENT)
        self.assertEqual(result.totals, {"new": 1, "reopened": 1, "status": 1})
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertIn("Open in Q1", message.subject)
        self.assertIn("brand new", message.body)
        self.assertIn("old one", message.body)
        self.assertIn("Reopened", message.body)
        self.assertNotIn("other queue ticket", message.body)

        self.search.refresh_from_db()
        self.assertEqual(self.search.digest_last_success_at, RUN_AT_DT)
        types = set(
            SavedSearchDigestEvent.objects.filter(saved_search=self.search).values_list(
                "event_type", flat=True
            )
        )
        self.assertEqual(types, {"new", "status", "reopened"})

    def test_empty_window_sends_nothing_but_advances_watermark(self):
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="earlier ticket")
        # First run delivers one e-mail and sets the watermark.
        with freeze_time(RUN_AT):
            first = send_digest_for_search(self.search)
        self.assertEqual(first.state, STATE_SENT)
        self.assertEqual(len(mail.outbox), 1)

        # Nothing happened in the meantime: no e-mail, watermark still moves.
        with freeze_time("2026-10-20 18:00:00"):
            second = send_digest_for_search(self.search)
        self.assertEqual(second.state, STATE_EMPTY)
        self.assertEqual(second.totals, {"new": 0, "reopened": 0, "status": 0})
        self.assertEqual(len(mail.outbox), 1)
        self.search.refresh_from_db()
        self.assertEqual(
            self.search.digest_last_success_at,
            datetime(2026, 10, 20, 18, 0, tzinfo=dt_timezone.utc),
        )

    def test_overlapping_window_delivers_each_change_only_once(self):
        with freeze_time("2026-10-19 06:00:00"):
            ticket = create_ticket(queue=self.queue, title="overlap ticket")
        f_resolved = self.status_followup(
            ticket, Ticket.RESOLVED_STATUS, "2026-10-20 10:00:00"
        )
        f_reopened = self.status_followup(
            ticket, Ticket.REOPENED_STATUS, "2026-10-20 11:00:00"
        )
        with freeze_time(RUN_AT):
            first = send_digest_for_search(self.search)
        self.assertEqual(first.state, STATE_SENT)

        # Pretend the scheduler reruns with an overlapping window: wind the
        # watermark back to before the already-delivered changes, and add one
        # genuinely new change inside the overlap.
        f_closed = self.status_followup(
            ticket, Ticket.CLOSED_STATUS, "2026-10-20 11:30:00"
        )
        SavedSearch.objects.filter(pk=self.search.pk).update(
            digest_last_success_at=datetime(2026, 10, 20, 0, 0, tzinfo=dt_timezone.utc)
        )

        with freeze_time("2026-10-20 14:00:00"):
            second = send_digest_for_search(self.search)

        self.assertEqual(second.state, STATE_SENT)
        self.assertEqual(second.totals, {"new": 0, "reopened": 0, "status": 1})
        second_body = mail.outbox[1].body
        # The new change is present; the previously delivered ones are not.
        self.assertIn("Closed", second_body)
        self.assertNotIn("Reopened", second_body)

        # Event log has exactly one row per delivered event (hard unique key).
        logged = set(
            SavedSearchDigestEvent.objects.filter(saved_search=self.search).values_list(
                "followup_id", flat=True
            )
        )
        self.assertEqual(logged, {f_resolved.pk, f_reopened.pk, f_closed.pk})

    def test_failed_send_keeps_watermark_and_retries_exactly_once(self):
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="retry ticket")

        with freeze_time(RUN_AT):
            with patch.object(
                mail.EmailMultiAlternatives,
                "send",
                side_effect=SMTPException("smtp down"),
            ):
                with self.assertRaises(DigestSendError):
                    send_digest_for_search(self.search)

        # Nothing was committed: no watermark, no event log, no delivered mail.
        self.search.refresh_from_db()
        self.assertIsNone(self.search.digest_last_success_at)
        self.assertEqual(
            SavedSearchDigestEvent.objects.filter(saved_search=self.search).count(),
            0,
        )
        self.assertEqual(len(mail.outbox), 0)

        # The retry covers the same window and delivers a single message.
        with freeze_time("2026-10-20 13:00:00"):
            retry = send_digest_for_search(self.search)

        self.assertEqual(retry.state, STATE_SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("retry ticket", mail.outbox[0].body)
        self.search.refresh_from_db()
        self.assertEqual(self.search.digest_last_success_at, RETRY_AT_DT)

        # Another retry immediately after success finds nothing to resend.
        with freeze_time("2026-10-20 14:00:00"):
            again = send_digest_for_search(self.search)
        self.assertEqual(again.state, STATE_EMPTY)
        self.assertEqual(len(mail.outbox), 1)

    def test_new_ticket_is_not_also_listed_as_status_change(self):
        with freeze_time("2026-10-20 08:00:00"):
            ticket = create_ticket(queue=self.queue, title="born and reopened")
        self.status_followup(ticket, Ticket.REOPENED_STATUS, "2026-10-20 09:00:00")

        with freeze_time(RUN_AT):
            result = send_digest_for_search(self.search)

        self.assertEqual(result.state, STATE_SENT)
        self.assertEqual(result.totals, {"new": 1, "reopened": 0, "status": 0})

    def test_dry_run_changes_nothing(self):
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="dry run ticket")

        with freeze_time(RUN_AT):
            result = send_digest_for_search(self.search, dry_run=True)

        self.assertEqual(result.state, STATE_WOULD_SEND)
        self.assertEqual(result.totals["new"], 1)
        self.assertEqual(len(mail.outbox), 0)
        self.search.refresh_from_db()
        self.assertIsNone(self.search.digest_last_success_at)
        self.assertEqual(
            SavedSearchDigestEvent.objects.filter(saved_search=self.search).count(),
            0,
        )

    def test_send_all_skips_disabled_searches(self):
        self.search.digest_enabled = False
        self.search.save()
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="ignored")

        self.assertEqual(send_all_digests(), [])
        self.assertEqual(len(mail.outbox), 0)

        self.search.digest_enabled = True
        self.search.save()
        with freeze_time(RUN_AT):
            results = send_all_digests()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, STATE_SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_missing_recipient_is_reported_as_failure(self):
        self.user.email = ""
        self.user.save()
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="nobody home")

        with freeze_time(RUN_AT):
            results = send_all_digests()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, STATE_FAILED)
        self.assertIn("e-mail address", results[0].error)
        self.assertEqual(len(mail.outbox), 0)
        self.search.refresh_from_db()
        self.assertIsNone(self.search.digest_last_success_at)

    def test_broken_stored_query_is_reported_as_failure(self):
        self.search.query = "this-is-not-base64-json"
        self.search.save()

        with freeze_time(RUN_AT):
            results = send_all_digests()

        self.assertEqual(results[0].state, STATE_FAILED)
        self.assertEqual(len(mail.outbox), 0)

    def test_command_exits_nonzero_when_a_digest_fails(self):
        self.user.email = ""
        self.user.save()
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="no recipient")

        with freeze_time(RUN_AT):
            with self.assertRaises(SystemExit) as cm:
                call_command("send_saved_search_digests")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_command_unknown_id_exits_nonzero(self):
        with self.assertRaises(SystemExit) as cm:
            call_command("send_saved_search_digests", "--id", "999999")
        self.assertEqual(cm.exception.code, 1)

    def test_command_happy_path(self):
        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="command ticket")

        with freeze_time(RUN_AT):
            call_command("send_saved_search_digests")

        self.assertEqual(len(mail.outbox), 1)
        self.search.refresh_from_db()
        self.assertEqual(self.search.digest_last_success_at, RUN_AT_DT)


class DigestToggleViewTestCase(TestCase):
    def setUp(self):
        self.user = get_user(is_staff=True)
        self.other_user = get_user(username="other.staff", is_staff=True)
        self.queue = Queue.objects.create(title="Q1", slug="q1")
        self.search = SavedSearch.objects.create(
            user=self.user,
            title="mine",
            query=query_to_base64({"filtering": {"queue__id__in": [self.queue.pk]}}),
        )
        self.url = reverse(
            "helpdesk:toggle_saved_search_digest", args=(self.search.pk,)
        )

    def test_enable_then_disable(self):
        self.client.login(username=self.user.get_username(), password="password")

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.search.refresh_from_db()
        self.assertTrue(self.search.digest_enabled)

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.search.refresh_from_db()
        self.assertFalse(self.search.digest_enabled)

    def test_get_not_allowed(self):
        self.client.login(username=self.user.get_username(), password="password")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_owner_only(self):
        # Another staff member neither owns nor shares this search: 404.
        self.client.login(username=self.other_user.get_username(), password="password")
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.search.refresh_from_db()
        self.assertFalse(self.search.digest_enabled)

    def test_saved_search_pages_and_filter_still_work(self):
        # The digest feature is additive: saved search listing and the manual
        # filter results keep working exactly as before.
        self.client.login(username=self.user.get_username(), password="password")

        response = self.client.get(reverse("helpdesk:saved_searches_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "mine")

        with freeze_time("2026-10-20 08:00:00"):
            create_ticket(queue=self.queue, title="visible manually")

        response = self.client.get(
            reverse("helpdesk:list"), {"saved_query": self.search.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["from_saved_query"])

        # The data table behind the manual filter still runs the stored query.
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[self.search.query])
        )
        self.assertEqual(response.status_code, 200)
        titles = [row["title"] for row in response.json()["data"]]
        self.assertIn("visible manually", titles)
