import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("helpdesk", "0043_fix_on_delete_behaviour"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebhookDelivery",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "event_id",
                    models.CharField(
                        db_index=True, editable=False, max_length=128, verbose_name="Event ID"
                    ),
                ),
                (
                    "event_type",
                    models.CharField(
                        choices=[("new_ticket", "New ticket"), ("followup", "Follow-up")],
                        max_length=32,
                        verbose_name="Event type",
                    ),
                ),
                ("url", models.URLField(max_length=500, verbose_name="Destination URL")),
                ("payload", models.JSONField(verbose_name="Payload")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("success", "Success"),
                            ("quarantined", "Quarantined"),
                        ],
                        default="pending",
                        max_length=16,
                        verbose_name="Status",
                    ),
                ),
                (
                    "attempts",
                    models.PositiveIntegerField(default=0, verbose_name="Attempts"),
                ),
                (
                    "not_before",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="Not before"
                    ),
                ),
                (
                    "last_error",
                    models.TextField(
                        blank=True, default="", verbose_name="Last failure reason"
                    ),
                ),
                (
                    "last_response_status",
                    models.PositiveIntegerField(
                        blank=True,
                        null=True,
                        verbose_name="Last HTTP response status",
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created"),
                ),
                (
                    "modified",
                    models.DateTimeField(auto_now=True, verbose_name="Modified"),
                ),
                (
                    "followup",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="webhook_deliveries",
                        to="helpdesk.followup",
                        verbose_name="Follow-up",
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="webhook_deliveries",
                        to="helpdesk.ticket",
                        verbose_name="Ticket",
                    ),
                ),
            ],
            options={
                "verbose_name": "Webhook delivery",
                "verbose_name_plural": "Webhook deliveries",
                "ordering": ("created",),
            },
        ),
        migrations.AddConstraint(
            model_name="webhookdelivery",
            constraint=models.UniqueConstraint(
                fields=("event_id", "url"),
                name="helpdesk_webhook_event_url_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="webhookdelivery",
            index=models.Index(fields=["status", "not_before"], name="helpdesk_we_status_39a87d_idx"),
        ),
    ]
