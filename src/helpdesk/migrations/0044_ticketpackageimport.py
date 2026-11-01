import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("helpdesk", "0043_fix_on_delete_behaviour"),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketPackageImport",
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
                    "package_id",
                    models.UUIDField(
                        db_index=True,
                        help_text="Stable identifier of the imported ticket package.",
                        verbose_name="Package ID",
                    ),
                ),
                (
                    "local_ref",
                    models.CharField(
                        help_text="Reference used inside the package for the source ticket.",
                        max_length=64,
                        verbose_name="Package-local ticket reference",
                    ),
                ),
                (
                    "imported_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Imported at"),
                ),
                (
                    "ticket",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="package_imports",
                        to="helpdesk.ticket",
                        verbose_name="Ticket",
                    ),
                ),
            ],
            options={
                "verbose_name": "Ticket package import",
                "verbose_name_plural": "Ticket package imports",
                "unique_together": {("package_id", "local_ref")},
            },
        ),
    ]
