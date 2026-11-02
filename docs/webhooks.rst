Webhooks
--------

You can register webhooks to allow third party apps to be notified of helpdesk events. Webhooks can be registered in one of two ways:

1. Setting the following environment variables to a comma separated list of URLs; ``HELPDESK_NEW_TICKET_WEBHOOK_URLS``& ``HELPDESK_FOLLOWUP_WEBHOOK_URLS``.

2. Adding getter functions to your ``settings.py``. These should return a list of strings (urls); ``HELPDESK_GET_NEW_TICKET_WEBHOOK_URLS`` & ``HELPDESK_GET_FOLLOWUP_WEBHOOK_URLS``.

3. You can optionally set ``HELPDESK_WEBHOOK_TIMEOUT`` which defaults to 3 seconds. Warning, however, webhook requests are sent out synchronously on ticket update. If your webhook handling server is too slow, you should fix this rather than causing helpdesk freezes by messing with this variable.

Once these URLs are configured, a serialized copy of the ticket object will be posted to each of these URLs each time a ticket is created or followed up on respectively.

Reliable delivery and retries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Webhooks are delivered through a small transactional outbox so a receiving
service being briefly unavailable no longer loses events:

1. When an event fires, one ``WebhookDelivery`` row per configured URL is
   saved to the database **before** anything is sent. If helpdesk restarts
   (or crashes) at any point, pending events survive and can be recovered.

2. The first delivery attempt is still made immediately when the event is
   emitted, preserving the historical synchronous behaviour. Set
   ``HELPDESK_WEBHOOK_SYNC_SEND = False`` to persist events without making an
   inline attempt and leave all delivery to the sweeper (useful to keep
   request latency off the ticket update path).

3. Failed attempts (network errors, timeouts, or non-2xx responses) are
   retried on an interval schedule:
   ``HELPDESK_WEBHOOK_RETRY_INTERVALS``, default ``(30, 60, 120, 300, 600)``
   seconds (the last value repeats). After ``HELPDESK_WEBHOOK_MAX_ATTEMPTS``
   (default 5) failed attempts the delivery is moved to the ``quarantined``
   state instead of being retried forever; its last failure reason and HTTP
   status are kept on the row. Quarantined deliveries can be inspected in the
   Django admin and requeued there, or retried explicitly with the
   ``--retry-quarantined`` switch.

Run the sweeper periodically (every minute or so) via cron or Celery beat::

    python manage.py send_webhook_deliveries
    # also replay isolated deliveries once the receiving side is fixed:
    python manage.py send_webhook_deliveries --retry-quarantined

If you use Celery, the same sweep is available as the
``helpdesk.tasks.helpdesk_send_webhook_deliveries`` task. Because the sweeper
only picks pending rows whose retry time has come and never touches
delivered rows, an outage only ever causes the missed events to be replayed.

Every request carries an ``Idempotency-Key`` header (also sent as
``X-Helpdesk-Event-Id``) whose value is stable across retries, e.g.
``helpdesk.followup.42``. Receiving services should store this value and skip
duplicate processing when the same key arrives again - delivery is
"at least once", so the same event can be delivered more than once. Further
headers identify the event type and the current attempt number:
``X-Helpdesk-Event-Type``, ``X-Helpdesk-Delivery-Id`` and
``X-Helpdesk-Attempt``.

Signals
-------

Webhooks are triggered through `Django Signals <https://docs.djangoproject.com/en/stable/topics/signals/>`_

The two available signals are
  - ``new_ticket_done``
  - ``update_ticket_done``

You have the opportunity to listen to those in your project if you have post processing workflows outside of webhooks::


  from django.dispatch import receiver
  from helpdesk.signals import new_ticket_done, update_ticket_done

  @receiver(new_ticket_done)
  def process_new_ticket(sender, ticket, **kwargs):
      "Triggers this code when a ticket is created."
      pass

  @receiver(update_ticket_done)
  def process_followup_update(sender, followup, **kwargs):
      "Triggers this code when a follow-up is created."
      pass
