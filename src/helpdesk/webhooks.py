import logging

import requests
import requests.exceptions
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from . import settings
from .models import WebhookDelivery
from .signals import new_ticket_done, update_ticket_done

logger = logging.getLogger(__name__)

#: Header carrying the stable event identity used by receivers to deduplicate.
IDEMPOTENCY_HEADER = "Idempotency-Key"
EVENT_ID_HEADER = "X-Helpdesk-Event-Id"
EVENT_TYPE_HEADER = "X-Helpdesk-Event-Type"
DELIVERY_ID_HEADER = "X-Helpdesk-Delivery-Id"
ATTEMPT_HEADER = "X-Helpdesk-Attempt"

#: How much of a non-success response body is kept on the quarantined row.
MAX_REASON_LENGTH = 1000


def _serialize_ticket(ticket):
    # Imported locally to keep the model import cycle light.
    from .serializers import TicketSerializer

    ticket.set_custom_field_values()
    return TicketSerializer(ticket).data


def _normalize_urls(urls):
    """Drop empty entries and surrounding whitespace from configured URLs."""
    if not urls:
        return []
    return [url.strip() for url in urls if url and url.strip()]


def _build_new_ticket_payload(ticket):
    ticket.set_custom_field_values()
    return {
        "event_id": _new_ticket_event_id(ticket),
        "ticket": _serialize_ticket(ticket),
        "queue_slug": ticket.queue.slug,
    }


def _build_followup_payload(followup):
    ticket = followup.ticket
    return {
        "event_id": _followup_event_id(followup),
        "ticket": _serialize_ticket(ticket),
        "queue_slug": ticket.queue.slug,
        "followup_id": followup.id,
    }


def _new_ticket_event_id(ticket):
    return f"helpdesk.new_ticket.{ticket.id}"


def _followup_event_id(followup):
    return f"helpdesk.followup.{followup.id}"


def enqueue_deliveries(event_type, event_id, urls, payload, ticket=None, followup=None):
    """Persist one pending delivery per destination, before any HTTP attempt.

    The (event_id, url) unique constraint makes this idempotent: a duplicate
    signal dispatch for the same logical event never creates a second row, and
    redeliveries of the row reuse the same Idempotency-Key.
    """
    deliveries = []
    for url in _normalize_urls(urls):
        delivery, created = WebhookDelivery.objects.get_or_create(
            event_id=event_id,
            url=url,
            defaults={
                "event_type": event_type,
                "payload": payload,
                "ticket": ticket,
                "followup": followup,
            },
        )
        if created:
            deliveries.append(delivery)
    return deliveries


def _headers_for(delivery):
    return {
        IDEMPOTENCY_HEADER: delivery.event_id,
        EVENT_ID_HEADER: delivery.event_id,
        EVENT_TYPE_HEADER: delivery.event_type,
        DELIVERY_ID_HEADER: str(delivery.id),
        ATTEMPT_HEADER: str(delivery.attempts),
    }


def attempt_delivery(delivery):
    """Perform one HTTP delivery attempt and persist the outcome.

    Returns True on success, False otherwise. Never raises: every failure is
    recorded on the row (and triggers a scheduled retry or quarantine), so a
    broken or slow external service can no longer break ticket processing.
    """
    # Record the attempt before opening the connection, so a crash while the
    # HTTP call is in flight still leaves an honest attempts count behind.
    delivery.attempts += 1
    delivery.save(update_fields=["attempts", "modified"])
    try:
        response = requests.post(
            delivery.url,
            json=delivery.payload,
            headers=_headers_for(delivery),
            timeout=settings.HELPDESK_WEBHOOK_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        reason = f"{exc.__class__.__name__}: {exc}"[:MAX_REASON_LENGTH]
        new_status = delivery.mark_failure(
            reason,
            max_attempts=settings.HELPDESK_WEBHOOK_MAX_ATTEMPTS,
            intervals=settings.HELPDESK_WEBHOOK_RETRY_INTERVALS,
        )
        logger.warning(
            "Webhook delivery %s to %s failed (attempt %s): %s",
            delivery.event_id,
            delivery.url,
            delivery.attempts,
            reason,
        )
        if new_status == WebhookDelivery.STATUS_QUARANTINED:
            logger.error(
                "Webhook delivery %s to %s quarantined after %s attempts: %s",
                delivery.event_id,
                delivery.url,
                delivery.attempts,
                reason,
            )
        return False

    if settings.HELPDESK_WEBHOOK_SUCCESS_STATUS(response.status_code):
        delivery.mark_success()
        logger.info(
            "Webhook delivery %s to %s succeeded on attempt %s",
            delivery.event_id,
            delivery.url,
            delivery.attempts,
        )
        return True

    reason = f"HTTP {response.status_code}: {response.text[:MAX_REASON_LENGTH]}"
    new_status = delivery.mark_failure(
        reason,
        response_status=response.status_code,
        max_attempts=settings.HELPDESK_WEBHOOK_MAX_ATTEMPTS,
        intervals=settings.HELPDESK_WEBHOOK_RETRY_INTERVALS,
    )
    logger.warning(
        "Webhook delivery %s to %s rejected with HTTP %s (attempt %s)",
        delivery.event_id,
        delivery.url,
        response.status_code,
        delivery.attempts,
    )
    if new_status == WebhookDelivery.STATUS_QUARANTINED:
        logger.error(
            "Webhook delivery %s to %s quarantined after %s attempts (last: %s)",
            delivery.event_id,
            delivery.url,
            delivery.attempts,
            reason,
        )
    return False


def _dispatch(event_type, event_id, urls, payload, ticket=None, followup=None):
    """Persist the event, then make the historical immediate first attempt."""
    deliveries = enqueue_deliveries(
        event_type, event_id, urls, payload, ticket=ticket, followup=followup
    )
    if not settings.HELPDESK_WEBHOOK_SYNC_SEND:
        return deliveries
    # The rows are durable now; a failure here simply leaves them pending for
    # the sweeper, it must never propagate into the ticket update flow.
    for delivery in deliveries:
        attempt_delivery(delivery)
    return deliveries


def _due_delivery_ids(now, limit, include_quarantined=False):
    queryset = WebhookDelivery.objects.all()
    if include_quarantined:
        # Explicit replay run: give quarantined rows a fresh retry series too.
        queryset = queryset.filter(
            status__in=[
                WebhookDelivery.STATUS_PENDING,
                WebhookDelivery.STATUS_QUARANTINED,
            ]
        )
    else:
        # Normal sweep: only pending rows whose retry interval has elapsed;
        # succeeded and quarantined rows are deliberately never touched.
        queryset = queryset.filter(status=WebhookDelivery.STATUS_PENDING)
    queryset = queryset.filter(not_before__lte=now).order_by("not_before", "id")
    return list(queryset.values_list("id", flat=True)[:limit])


def send_due_deliveries(limit=None, include_quarantined=False):
    """Deliver pending rows whose retry time has come.

    This is the restart-safe recovery path: all state lives in the database,
    so a fresh process (cron management command or Celery task) picks up
    everything that was pending or quarantined when the previous one stopped.

    Returns a summary dict ``{processed, succeeded, failed, quarantined}``.
    """
    now = timezone.now()
    if limit is None:
        limit = settings.HELPDESK_WEBHOOK_BATCH_SIZE

    summary = {"processed": 0, "succeeded": 0, "failed": 0, "quarantined": 0}
    for delivery_id in _due_delivery_ids(now, limit, include_quarantined):
        with transaction.atomic():
            # Lock the row so concurrent sweeper workers can't double-send it.
            try:
                delivery = WebhookDelivery.objects.select_for_update(
                    skip_locked=True
                ).get(id=delivery_id)
            except WebhookDelivery.DoesNotExist:
                continue
            if delivery.status == WebhookDelivery.STATUS_SUCCESS:
                continue
            if (
                delivery.status == WebhookDelivery.STATUS_PENDING
                and delivery.not_before > now
            ):
                continue
            if (
                delivery.status == WebhookDelivery.STATUS_QUARANTINED
                and not include_quarantined
            ):
                continue
            if delivery.status == WebhookDelivery.STATUS_QUARANTINED:
                # Fresh series of attempts for the manually requested replay.
                delivery.requeue()

        summary["processed"] += 1
        if attempt_delivery(delivery):
            summary["succeeded"] += 1
        else:
            summary["failed"] += 1
            delivery.refresh_from_db(fields=["status"])
            if delivery.status == WebhookDelivery.STATUS_QUARANTINED:
                summary["quarantined"] += 1
    return summary


def notify_followup_webhooks(followup):
    urls = settings.HELPDESK_GET_FOLLOWUP_WEBHOOK_URLS()
    if not urls:
        return
    payload = _build_followup_payload(followup)
    _dispatch(
        WebhookDelivery.EVENT_FOLLOWUP,
        payload["event_id"],
        urls,
        payload,
        ticket=followup.ticket,
        followup=followup,
    )


# listener is loaded via app.py HelpdeskConfig.ready()
@receiver(update_ticket_done)
def notify_followup_webhooks_receiver(sender, followup, **kwargs):
    notify_followup_webhooks(followup)


def send_new_ticket_webhook(ticket):
    urls = settings.HELPDESK_GET_NEW_TICKET_WEBHOOK_URLS()
    if not urls:
        return
    payload = _build_new_ticket_payload(ticket)
    _dispatch(
        WebhookDelivery.EVENT_NEW_TICKET,
        payload["event_id"],
        urls,
        payload,
        ticket=ticket,
    )


# listener is loaded via app.py HelpdeskConfig.ready()
@receiver(new_ticket_done)
def send_new_ticket_webhook_receiver(sender, ticket, **kwargs):
    send_new_ticket_webhook(ticket)
