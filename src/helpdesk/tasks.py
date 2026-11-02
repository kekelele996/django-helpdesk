from celery import shared_task

from .email import process_email


@shared_task
def helpdesk_process_email():
    process_email()


@shared_task
def helpdesk_send_webhook_deliveries():
    # Imported here so the task module stays light for workers that only poll
    # email. Schedule this periodically to retry webhooks that failed inline.
    from .webhooks import send_due_deliveries

    return send_due_deliveries()
