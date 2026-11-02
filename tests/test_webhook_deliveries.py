"""Tests for the durable webhook delivery pipeline (outbox, retries,
idempotency keys, quarantine and restart recovery)."""

import http.server
import json
import threading
from contextlib import ExitStack
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.test import TestCase
from freezegun import freeze_time

from helpdesk import settings as helpdesk_settings
from helpdesk.models import FollowUp, Queue, Ticket, WebhookDelivery
from helpdesk.signals import new_ticket_done, update_ticket_done
from helpdesk.webhooks import (
    attempt_delivery,
    enqueue_deliveries,
    send_due_deliveries,
)

HOOK_URL = "http://example.invalid/hook"
OTHER_URL = "http://example.invalid/other"
BASE_TIME = datetime(2026, 11, 2, 12, 0, 0, tzinfo=dt_timezone.utc)


def _response(status_code=200, text="ok"):
    return mock.Mock(status_code=status_code, text=text)


class WebhookDeliveryTestBase(TestCase):
    def setUp(self):
        super().setUp()
        self.queue = Queue.objects.create(title="Queue", slug="queue-webhook")
        self.ticket = Ticket.objects.create(title="A ticket", queue=self.queue)
        self.followup = FollowUp.objects.create(
            ticket=self.ticket, title="Updated", comment="a comment"
        )
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Point the configured getters at a single test URL by default.
        self.set_urls(new_ticket_urls=[HOOK_URL], followup_urls=[HOOK_URL])

    def patch_settings(self, **kwargs):
        for name, value in kwargs.items():
            self.stack.enter_context(
                mock.patch.object(helpdesk_settings, name, value, create=True)
            )

    def set_urls(self, new_ticket_urls=None, followup_urls=None):
        if new_ticket_urls is not None:
            self.stack.enter_context(
                mock.patch.object(
                    helpdesk_settings,
                    "HELPDESK_GET_NEW_TICKET_WEBHOOK_URLS",
                    lambda: list(new_ticket_urls),
                )
            )
        if followup_urls is not None:
            self.stack.enter_context(
                mock.patch.object(
                    helpdesk_settings,
                    "HELPDESK_GET_FOLLOWUP_WEBHOOK_URLS",
                    lambda: list(followup_urls),
                )
            )

    def patch_post(self, side_effect=None, return_value=None):
        post = mock.Mock(
            side_effect=side_effect,
            return_value=return_value if return_value is not None else _response(),
        )
        # A side_effect and a return_value are mutually exclusive on Mock.
        if side_effect is not None:
            post = mock.Mock(side_effect=side_effect)
        self.stack.enter_context(mock.patch("helpdesk.webhooks.requests.post", post))
        return post


class DurableDeliveryTests(WebhookDeliveryTestBase):
    def test_event_persisted_then_marked_success_with_headers(self):
        post = self.patch_post()
        new_ticket_done.send(sender="test", ticket=self.ticket)

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.event_id, f"helpdesk.new_ticket.{self.ticket.id}")
        self.assertEqual(delivery.event_type, WebhookDelivery.EVENT_NEW_TICKET)
        self.assertEqual(delivery.url, HOOK_URL)
        self.assertEqual(delivery.payload["event_id"], delivery.event_id)
        self.assertEqual(delivery.payload["ticket"]["id"], self.ticket.id)
        self.assertEqual(delivery.ticket_id, self.ticket.id)

        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], delivery.event_id)
        self.assertEqual(kwargs["headers"]["X-Helpdesk-Event-Id"], delivery.event_id)
        self.assertEqual(
            kwargs["headers"]["X-Helpdesk-Event-Type"],
            WebhookDelivery.EVENT_NEW_TICKET,
        )
        self.assertEqual(kwargs["headers"]["X-Helpdesk-Attempt"], "1")
        self.assertEqual(kwargs["json"]["event_id"], delivery.event_id)

    def test_network_failure_is_recorded_and_retried_after_interval(self):
        self.patch_post(side_effect=requests.exceptions.ConnectionError("service down"))
        self.patch_settings(HELPDESK_WEBHOOK_RETRY_INTERVALS=(30, 60))

        freezer = freeze_time(BASE_TIME)
        frozen = freezer.start()
        self.addCleanup(freezer.stop)

        update_ticket_done.send(sender="test", followup=self.followup)

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(delivery.attempts, 1)
        self.assertIn("ConnectionError", delivery.last_error)
        self.assertIn("service down", delivery.last_error)
        self.assertEqual(delivery.not_before, BASE_TIME + timedelta(seconds=30))

        # Retry interval has not elapsed: nothing is sent.
        self.assertEqual(
            send_due_deliveries(),
            {"processed": 0, "succeeded": 0, "failed": 0, "quarantined": 0},
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 1)

        frozen.move_to(BASE_TIME + timedelta(seconds=31))
        summary = send_due_deliveries()
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["failed"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(delivery.not_before, BASE_TIME + timedelta(seconds=31 + 60))

        # Service recovered: only the still-pending event is replayed once.
        self.patch_post(return_value=_response(200))
        frozen.move_to(BASE_TIME + timedelta(seconds=100))
        summary = send_due_deliveries()
        self.assertEqual(summary["succeeded"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)
        self.assertEqual(delivery.last_error, "")
        self.assertIsNone(delivery.last_response_status)

        # Delivered events are never sent again.
        frozen.move_to(BASE_TIME + timedelta(days=1))
        self.assertEqual(send_due_deliveries()["processed"], 0)

    def test_http_error_status_is_treated_as_failure(self):
        post = self.patch_post(return_value=_response(503, text="maintenance"))
        self.patch_settings(HELPDESK_WEBHOOK_RETRY_INTERVALS=(0,))

        new_ticket_done.send(sender="test", ticket=self.ticket)

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_response_status, 503)
        self.assertIn("HTTP 503", delivery.last_error)
        self.assertIn("maintenance", delivery.last_error)

        post.return_value = _response(204)
        self.assertEqual(send_due_deliveries()["succeeded"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)

    def test_quarantine_after_max_attempts_keeps_last_reason(self):
        self.patch_post(side_effect=requests.exceptions.Timeout("still down"))
        self.patch_settings(
            HELPDESK_WEBHOOK_MAX_ATTEMPTS=3,
            HELPDESK_WEBHOOK_RETRY_INTERVALS=(10, 20, 30),
        )

        freezer = freeze_time(BASE_TIME)
        frozen = freezer.start()
        self.addCleanup(freezer.stop)

        enqueue_deliveries(
            WebhookDelivery.EVENT_FOLLOWUP,
            f"helpdesk.followup.{self.followup.id}",
            [HOOK_URL],
            {"event_id": f"helpdesk.followup.{self.followup.id}"},
            followup=self.followup,
        )

        # Walk the retry schedule: attempts 1 and 2 keep retrying...
        send_due_deliveries()
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(delivery.attempts, 1)

        frozen.move_to(BASE_TIME + timedelta(seconds=11))
        send_due_deliveries()
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(delivery.attempts, 2)

        # ...attempt 3 exhausts the budget and isolates the row.
        frozen.move_to(BASE_TIME + timedelta(seconds=32))
        summary = send_due_deliveries()
        self.assertEqual(summary["quarantined"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_QUARANTINED)
        self.assertEqual(delivery.attempts, 3)
        self.assertIn("Timeout", delivery.last_error)

        # Ordinary sweeps leave quarantined rows alone, however late.
        frozen.move_to(BASE_TIME + timedelta(days=2))
        self.assertEqual(send_due_deliveries()["processed"], 0)
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 3)
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_QUARANTINED)

    def test_quarantined_replay_starts_fresh_series(self):
        self.patch_post(side_effect=requests.exceptions.ConnectionError("nope"))
        self.patch_settings(
            HELPDESK_WEBHOOK_MAX_ATTEMPTS=2,
            HELPDESK_WEBHOOK_RETRY_INTERVALS=(0,),
        )
        enqueue_deliveries(
            WebhookDelivery.EVENT_NEW_TICKET,
            f"helpdesk.new_ticket.{self.ticket.id}",
            [HOOK_URL],
            {"event_id": f"helpdesk.new_ticket.{self.ticket.id}"},
            ticket=self.ticket,
        )
        send_due_deliveries()
        send_due_deliveries()
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_QUARANTINED)
        self.assertEqual(delivery.attempts, 2)

        # Explicit replay: endpoint still down -> fresh series, back to pending.
        summary = send_due_deliveries(include_quarantined=True)
        self.assertEqual(summary["processed"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(delivery.attempts, 1)

        # Endpoint recovers, a second explicit replay succeeds.
        self.patch_post(return_value=_response(200))
        summary = send_due_deliveries(include_quarantined=True)
        self.assertEqual(summary["succeeded"], 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)

    def test_sweeper_only_replays_missed_events(self):
        # One delivered event, one not-yet-due event, one due failed event.
        delivered = WebhookDelivery.objects.create(
            event_id="helpdesk.new_ticket.1",
            event_type=WebhookDelivery.EVENT_NEW_TICKET,
            url=HOOK_URL,
            payload={"event_id": "helpdesk.new_ticket.1"},
            status=WebhookDelivery.STATUS_SUCCESS,
            not_before=BASE_TIME,
        )
        sleeping = WebhookDelivery.objects.create(
            event_id="helpdesk.new_ticket.2",
            event_type=WebhookDelivery.EVENT_NEW_TICKET,
            url=HOOK_URL,
            payload={"event_id": "helpdesk.new_ticket.2"},
            not_before=BASE_TIME + timedelta(hours=1),
        )
        due = WebhookDelivery.objects.create(
            event_id="helpdesk.new_ticket.3",
            event_type=WebhookDelivery.EVENT_NEW_TICKET,
            url=OTHER_URL,
            payload={"event_id": "helpdesk.new_ticket.3"},
            attempts=1,
            not_before=BASE_TIME - timedelta(minutes=5),
        )
        post = self.patch_post()

        with freeze_time(BASE_TIME):
            summary = send_due_deliveries()

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(
            post.call_args.kwargs["json"]["event_id"], "helpdesk.new_ticket.3"
        )
        delivered.refresh_from_db()
        sleeping.refresh_from_db()
        due.refresh_from_db()
        self.assertEqual(delivered.status, WebhookDelivery.STATUS_SUCCESS)
        self.assertEqual(sleeping.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(sleeping.attempts, 0)
        self.assertEqual(due.status, WebhookDelivery.STATUS_SUCCESS)

    def test_idempotency_key_stable_and_enqueue_is_deduplicated(self):
        post = self.patch_post(
            side_effect=[
                requests.exceptions.ConnectionError("blip"),
                _response(200),
            ]
        )
        self.patch_settings(HELPDESK_WEBHOOK_RETRY_INTERVALS=(0,))
        event_id = f"helpdesk.followup.{self.followup.id}"

        # Duplicate signal dispatch (same logical event + same URL) must not
        # create a second row.
        deliveries_1 = enqueue_deliveries(
            WebhookDelivery.EVENT_FOLLOWUP,
            event_id,
            [HOOK_URL],
            {"event_id": event_id},
            followup=self.followup,
        )
        deliveries_2 = enqueue_deliveries(
            WebhookDelivery.EVENT_FOLLOWUP,
            event_id,
            [HOOK_URL],
            {"event_id": event_id},
            followup=self.followup,
        )
        self.assertEqual(len(deliveries_1), 1)
        self.assertEqual(deliveries_2, [])
        self.assertEqual(WebhookDelivery.objects.count(), 1)

        delivery = WebhookDelivery.objects.get()
        attempt_delivery(delivery)
        attempt_delivery(WebhookDelivery.objects.get(pk=delivery.pk))

        keys = {
            call.kwargs["headers"]["Idempotency-Key"] for call in post.call_args_list
        }
        self.assertEqual(keys, {event_id})
        delivery_ids = {
            call.kwargs["headers"]["X-Helpdesk-Delivery-Id"]
            for call in post.call_args_list
        }
        self.assertEqual(delivery_ids, {str(delivery.id)})
        attempts = [
            call.kwargs["headers"]["X-Helpdesk-Attempt"] for call in post.call_args_list
        ]
        self.assertEqual(attempts, ["1", "2"])
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)

    def test_different_destinations_share_event_id(self):
        post = self.patch_post()
        self.set_urls(new_ticket_urls=[HOOK_URL, OTHER_URL])

        new_ticket_done.send(sender="test", ticket=self.ticket)

        deliveries = WebhookDelivery.objects.filter(
            event_id=f"helpdesk.new_ticket.{self.ticket.id}"
        )
        self.assertEqual(
            set(deliveries.values_list("url", flat=True)), {HOOK_URL, OTHER_URL}
        )
        keys = {
            call.kwargs["headers"]["Idempotency-Key"] for call in post.call_args_list
        }
        self.assertEqual(keys, {f"helpdesk.new_ticket.{self.ticket.id}"})

    def test_sync_send_disabled_only_persists(self):
        post = self.patch_post()
        self.patch_settings(HELPDESK_WEBHOOK_SYNC_SEND=False)

        new_ticket_done.send(sender="test", ticket=self.ticket)
        self.assertFalse(post.called)

        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(delivery.attempts, 0)

        # The sweeper picks the persisted event up after a restart.
        summary = send_due_deliveries()
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(post.call_count, 1)

    def test_signal_never_raises_when_endpoint_is_down(self):
        self.patch_post(side_effect=requests.exceptions.ConnectionError("down"))
        # Ticket processing must complete normally despite the dead endpoint.
        new_ticket_done.send(sender="test", ticket=self.ticket)
        update_ticket_done.send(sender="test", followup=self.followup)
        self.assertEqual(WebhookDelivery.objects.count(), 2)
        self.assertTrue(
            WebhookDelivery.objects.exclude(
                status=WebhookDelivery.STATUS_SUCCESS
            ).exists()
        )


class LoopbackRedeliveryTest(TestCase):
    """End-to-end check against a real (loopback only) HTTP server."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            key = self.headers["Idempotency-Key"]
            seen = self.server.attempts.setdefault(key, 0)
            self.server.attempts[key] = seen + 1
            self.server.requests.append(
                {
                    "key": key,
                    "event_type": self.headers["X-Helpdesk-Event-Type"],
                    "attempt": self.headers["X-Helpdesk-Attempt"],
                    "body": json.loads(body.decode()),
                }
            )
            # Fail the first delivery of each event, like a short maintenance.
            if seen == 0:
                self.send_response(503)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()

    def setUp(self):
        super().setUp()
        self.queue = Queue.objects.create(title="Queue", slug="queue-loopback")
        self.ticket = Ticket.objects.create(title="Loopback ticket", queue=self.queue)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), self.Handler)
        self.server.attempts = {}
        self.server.requests = []
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_redelivery_uses_same_key_and_payload(self):
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    helpdesk_settings,
                    "HELPDESK_GET_NEW_TICKET_WEBHOOK_URLS",
                    lambda: [self.url],
                )
            )
            # Retry due immediately so the sweeper can recover without sleeping.
            stack.enter_context(
                mock.patch.object(
                    helpdesk_settings, "HELPDESK_WEBHOOK_RETRY_INTERVALS", (0,)
                )
            )
            new_ticket_done.send(sender="test", ticket=self.ticket)

            delivery = WebhookDelivery.objects.get()
            self.assertEqual(delivery.status, WebhookDelivery.STATUS_PENDING)
            self.assertEqual(delivery.attempts, 1)
            self.assertEqual(delivery.last_response_status, 503)

            summary = send_due_deliveries()
            self.assertEqual(summary["succeeded"], 1)

        self.assertEqual(len(self.server.requests), 2)
        first, second = self.server.requests
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(first["key"], f"helpdesk.new_ticket.{self.ticket.id}")
        self.assertEqual(first["event_type"], "new_ticket")
        self.assertEqual([first["attempt"], second["attempt"]], ["1", "2"])
        self.assertEqual(first["body"], second["body"])

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, WebhookDelivery.STATUS_SUCCESS)

    def test_management_command_recovers_pending_after_restart(self):
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    helpdesk_settings,
                    "HELPDESK_GET_FOLLOWUP_WEBHOOK_URLS",
                    lambda: [self.url],
                )
            )
            stack.enter_context(
                mock.patch.object(
                    helpdesk_settings, "HELPDESK_WEBHOOK_RETRY_INTERVALS", (0,)
                )
            )
            followup = FollowUp.objects.create(
                ticket=self.ticket, title="Updated", comment="ping"
            )
            update_ticket_done.send(sender="test", followup=followup)
            self.assertEqual(WebhookDelivery.objects.get().status, "pending")

            # A fresh process only knows the database state: run the command as
            # cron would after helpdesk restarted.
            out = StringIO()
            call_command("send_webhook_deliveries", stdout=out)
            self.assertIn("1 succeeded", out.getvalue())

        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(WebhookDelivery.objects.get().status, "success")
