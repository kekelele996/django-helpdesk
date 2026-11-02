import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

from helpdesk import settings as helpdesk_settings


class Migration(migrations.Migration):
    dependencies = [
        ("helpdesk", "0043_fix_on_delete_behaviour"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ] + helpdesk_settings.HELPDESK_TEAMS_MIGRATION_DEPENDENCIES

    operations = [
        migrations.AddField(
            model_name="savedsearch",
            name="digest_enabled",
            field=models.BooleanField(
                blank=True,
                default=False,
                help_text=(
                    "Send the owner a daily e-mail with tickets that are new, "
                    "changed status or were reopened since the last digest."
                ),
                verbose_name="Daily digest enabled",
            ),
        ),
        migrations.AddField(
            model_name="savedsearch",
            name="digest_last_success_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Point in time covered by the last digest e-mail.",
                verbose_name="Last successful digest at",
            ),
        ),
        migrations.CreateModel(
            name="SavedSearchDigestEvent",
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
                    "event_type",
                    models.CharField(
                        choices=[
                            ("new", "New ticket"),
                            ("status", "Status change"),
                            ("reopened", "Reopened"),
                        ],
                        max_length=20,
                        verbose_name="Event type",
                    ),
                ),
                (
                    "sent_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name="Sent at",
                    ),
                ),
                (
                    "followup",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="helpdesk.followup",
                        verbose_name="Follow-up",
                    ),
                ),
                (
                    "saved_search",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="digest_events",
                        to="helpdesk.savedsearch",
                        verbose_name="Saved search",
                    ),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="helpdesk.ticket",
                        verbose_name="Ticket",
                    ),
                ),
            ],
            options={
                "verbose_name": "Saved search digest event",
                "verbose_name_plural": "Saved search digest events",
                "unique_together": {
                    ("saved_search", "ticket", "followup", "event_type"),
                },
            },
        ),
    ]
