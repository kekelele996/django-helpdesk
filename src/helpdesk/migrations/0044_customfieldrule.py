from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("helpdesk", "0043_fix_on_delete_behaviour"),
    ]

    operations = [
        migrations.CreateModel(
            name="CustomFieldRule",
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
                    "display_when",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text=(
                            "Predicates (JSON list, all must match) under which "
                            'the field is shown. Example: [{"field": '
                            '"custom_fault_type", "operator": "eq", "value": '
                            '"network"}]. Empty means always shown.'
                        ),
                        verbose_name="Display when",
                    ),
                ),
                (
                    "required_when",
                    models.JSONField(
                        blank=True,
                        default=list,
                        help_text=(
                            "Predicates (JSON list, all must match) under which "
                            "the field is required. Empty falls back to the "
                            "field's static Required flag."
                        ),
                        verbose_name="Required when",
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(default=True, verbose_name="Enabled?"),
                ),
                (
                    "field",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rules",
                        to="helpdesk.customfield",
                        verbose_name="Custom field",
                    ),
                ),
                (
                    "queue",
                    models.ForeignKey(
                        blank=True,
                        help_text=(
                            "Leave empty for a rule that applies in every queue. "
                            "A queue-specific rule overrides the global rule for "
                            "the field."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customfield_rules",
                        to="helpdesk.queue",
                        verbose_name="Queue",
                    ),
                ),
            ],
            options={
                "verbose_name": "Custom field rule",
                "verbose_name_plural": "Custom field rules",
                "unique_together": {("field", "queue")},
            },
        ),
    ]
