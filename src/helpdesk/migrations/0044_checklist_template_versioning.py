import django.db.models.deletion
import django.utils.timezone
import helpdesk.models
from django.db import migrations, models


def backfill_template_versions(apps, schema_editor):
    ChecklistTemplate = apps.get_model("helpdesk", "ChecklistTemplate")
    ChecklistTemplateVersion = apps.get_model(
        "helpdesk", "ChecklistTemplateVersion"
    )
    Checklist = apps.get_model("helpdesk", "Checklist")
    ChecklistTask = apps.get_model("helpdesk", "ChecklistTask")

    # Every template that existed before versioning starts its history at v1.
    templates = list(ChecklistTemplate.objects.all())
    for template in templates:
        ChecklistTemplateVersion.objects.create(
            template=template,
            version=1,
            task_list=template.task_list,
        )

    # Best-effort link of legacy checklists to their source template: only
    # checklists whose name and ordered task descriptions match exactly one
    # template v1 are linked. Anything else stays an unlinked, frozen copy.
    for checklist in Checklist.objects.filter(
        template__isnull=True, template_version__isnull=True
    ):
        descriptions = list(
            ChecklistTask.objects.filter(checklist=checklist)
            .order_by("position", "id")
            .values_list("description", flat=True)
        )
        matches = [
            template
            for template in templates
            if template.name == checklist.name
            and list(template.task_list) == descriptions
        ]
        if len(matches) == 1:
            checklist.template = matches[0]
            checklist.template_version = 1
            checklist.save(update_fields=["template", "template_version"])


class Migration(migrations.Migration):

    dependencies = [
        ("helpdesk", "0043_fix_on_delete_behaviour"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklist",
            name="template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="checklists",
                to="helpdesk.checklisttemplate",
                verbose_name="Checklist Template",
            ),
        ),
        migrations.AddField(
            model_name="checklist",
            name="template_version",
            field=models.PositiveIntegerField(
                blank=True, null=True, verbose_name="Checklist Template Version"
            ),
        ),
        migrations.AddField(
            model_name="checklisttask",
            name="invalidated_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "When set, the task no longer belongs to the checklist's "
                    "template version but is kept for historical reference."
                ),
                null=True,
                verbose_name="Invalidated At",
            ),
        ),
        migrations.AddField(
            model_name="checklisttemplate",
            name="current_version",
            field=models.PositiveIntegerField(
                default=1, verbose_name="Current Version"
            ),
        ),
        migrations.CreateModel(
            name="ChecklistTemplateVersion",
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
                ("version", models.PositiveIntegerField(verbose_name="Version")),
                (
                    "task_list",
                    models.JSONField(
                        validators=[helpdesk.models.is_a_list_without_empty_element],
                        verbose_name="Task List",
                    ),
                ),
                (
                    "created",
                    models.DateTimeField(
                        default=django.utils.timezone.now, verbose_name="Created"
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="helpdesk.checklisttemplate",
                        verbose_name="Checklist Template",
                    ),
                ),
            ],
            options={
                "verbose_name": "Checklist Template Version",
                "verbose_name_plural": "Checklist Template Versions",
                "ordering": ("version",),
                "unique_together": {("template", "version")},
            },
        ),
        migrations.RunPython(
            backfill_template_versions, migrations.RunPython.noop
        ),
    ]
