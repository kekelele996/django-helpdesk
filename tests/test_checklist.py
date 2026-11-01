from _datetime import timedelta
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from helpdesk.models import (
    Checklist,
    ChecklistTask,
    ChecklistTemplate,
    ChecklistTemplateVersion,
    Queue,
    Ticket,
)


class TicketChecklistTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = get_user_model().objects.create_user("User", password="pass")
        user.is_staff = True
        user.save()
        cls.user = user

    def setUp(self) -> None:
        self.client.login(username="User", password="pass")

        self.ticket = Ticket.objects.create(
            queue=Queue.objects.create(title="Queue", slug="queue"), title="Test Queue"
        )
        self.default_update_post_data = {
            "queue": self.ticket.queue_id,
            "title": self.ticket.title,
            "priority": self.ticket.priority,
        }

    def test_create_checklist(self):
        self.assertEqual(self.ticket.checklists.count(), 0)
        checklist_name = "test empty checklist"

        response = self.client.post(
            reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}),
            data={"name": checklist_name},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_form.html")
        self.assertContains(response, checklist_name)

        self.assertEqual(self.ticket.checklists.count(), 1)

    def test_create_checklist_from_template(self):
        self.assertEqual(self.ticket.checklists.count(), 0)
        checklist_name = "test checklist from template"

        checklist_template = ChecklistTemplate.objects.create(
            name="Test template", task_list=["first", "second", "last"]
        )

        response = self.client.post(
            reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}),
            data={"name": checklist_name, "checklist_template": checklist_template.id},
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_form.html")
        self.assertContains(response, checklist_name)

        self.assertEqual(self.ticket.checklists.count(), 1)
        created_checklist = self.ticket.checklists.get()
        self.assertEqual(created_checklist.tasks.count(), 3)
        self.assertEqual(created_checklist.tasks.all()[0].description, "first")
        self.assertEqual(created_checklist.tasks.all()[1].description, "second")
        self.assertEqual(created_checklist.tasks.all()[2].description, "last")

    def test_edit_checklist(self):
        checklist = self.ticket.checklists.create(name="Test checklist")
        first_task = checklist.tasks.create(description="First task", position=1)
        checklist.tasks.create(description="To delete task", position=2)

        url = reverse(
            "helpdesk:edit_ticket_checklist",
            kwargs={
                "ticket_id": self.ticket.id,
                "checklist_id": checklist.id,
            },
        )

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_form.html")
        self.assertContains(response, "Test checklist")
        self.assertContains(response, "First task")
        self.assertContains(response, "To delete task")

        response = self.client.post(
            url,
            data={
                "name": "New name",
                "tasks-TOTAL_FORMS": 3,
                "tasks-INITIAL_FORMS": 2,
                "tasks-0-id": "1",
                "tasks-0-description": "First task edited",
                "tasks-0-position": "2",
                "tasks-1-id": "2",
                "tasks-1-description": "To delete task",
                "tasks-1-DELETE": "on",
                "tasks-1-position": "2",
                "tasks-2-description": "New first task",
                "tasks-2-position": "1",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/ticket.html")

        checklist.refresh_from_db()
        self.assertEqual(checklist.name, "New name")
        self.assertEqual(checklist.tasks.count(), 2)
        first_task.refresh_from_db()
        self.assertEqual(first_task.description, "First task edited")
        self.assertEqual(checklist.tasks.all()[0].description, "New first task")
        self.assertEqual(checklist.tasks.all()[1].description, "First task edited")

    def test_delete_checklist(self):
        checklist = self.ticket.checklists.create(name="Test checklist")
        checklist.tasks.create(description="First task", position=1)
        self.assertEqual(Checklist.objects.count(), 1)
        self.assertEqual(ChecklistTask.objects.count(), 1)

        response = self.client.post(
            reverse(
                "helpdesk:delete_ticket_checklist",
                kwargs={"ticket_id": self.ticket.id, "checklist_id": checklist.id},
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/ticket.html")

        self.assertEqual(Checklist.objects.count(), 0)
        self.assertEqual(ChecklistTask.objects.count(), 0)

    def test_mark_task_as_done(self):
        checklist = self.ticket.checklists.create(name="Test checklist")
        task = checklist.tasks.create(description="Task", position=1)
        self.assertIsNone(task.completion_date)

        self.assertEqual(self.ticket.followup_set.count(), 0)

        response = self.client.post(
            reverse("helpdesk:update", kwargs={"ticket_id": self.ticket.id}),
            data={
                f"checklist-{checklist.id}": task.id,
                **self.default_update_post_data,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/ticket.html")

        self.assertEqual(self.ticket.followup_set.count(), 1)
        followup = self.ticket.followup_set.get()
        self.assertEqual(followup.ticketchange_set.count(), 1)
        self.assertEqual(followup.ticketchange_set.get().old_value, "To do")
        self.assertEqual(followup.ticketchange_set.get().new_value, "Completed")

        task.refresh_from_db()
        self.assertIsNotNone(task.completion_date)

    def test_mark_task_as_undone(self):
        checklist = self.ticket.checklists.create(name="Test checklist")
        task = checklist.tasks.create(
            description="Task",
            position=1,
            completion_date=timezone.now() - timedelta(days=10),
        )
        self.assertIsNotNone(task.completion_date)

        self.assertEqual(self.ticket.followup_set.count(), 0)

        response = self.client.post(
            reverse("helpdesk:update", kwargs={"ticket_id": self.ticket.id}),
            data={**self.default_update_post_data},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/ticket.html")

        self.assertEqual(self.ticket.followup_set.count(), 1)
        followup = self.ticket.followup_set.get()
        self.assertEqual(followup.ticketchange_set.count(), 1)
        self.assertEqual(followup.ticketchange_set.get().old_value, "Completed")
        self.assertEqual(followup.ticketchange_set.get().new_value, "To do")

        task.refresh_from_db()
        self.assertIsNone(task.completion_date)

    def test_display_checklist_templates(self):
        ChecklistTemplate.objects.create(
            name="Test checklist template", task_list=["first", "second", "third"]
        )

        response = self.client.get(reverse("helpdesk:checklist_templates"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_templates.html")
        self.assertContains(response, "Test checklist template")
        self.assertContains(response, "3 tasks")

    def test_create_checklist_template(self):
        self.assertEqual(ChecklistTemplate.objects.count(), 0)

        response = self.client.post(
            reverse("helpdesk:checklist_templates"),
            data={
                "name": "Test checklist template",
                "task_list": '["first", "second", "third"]',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_templates.html")

        self.assertEqual(ChecklistTemplate.objects.count(), 1)
        checklist_template = ChecklistTemplate.objects.get()
        self.assertEqual(checklist_template.name, "Test checklist template")
        self.assertEqual(checklist_template.task_list, ["first", "second", "third"])

    def test_edit_checklist_template(self):
        checklist_template = ChecklistTemplate.objects.create(
            name="Test checklist template", task_list=["first", "second", "third"]
        )
        self.assertEqual(ChecklistTemplate.objects.count(), 1)

        response = self.client.post(
            reverse(
                "helpdesk:edit_checklist_template",
                kwargs={"checklist_template_id": checklist_template.id},
            ),
            data={
                "name": "New checklist template",
                "task_list": '["new first", "second", "third", "last"]',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_templates.html")

        self.assertEqual(ChecklistTemplate.objects.count(), 1)
        checklist_template.refresh_from_db()
        self.assertEqual(checklist_template.name, "New checklist template")
        self.assertEqual(
            checklist_template.task_list, ["new first", "second", "third", "last"]
        )

    def test_delete_checklist_template(self):
        checklist_template = ChecklistTemplate.objects.create(
            name="Test checklist template", task_list=["first", "second", "third"]
        )
        self.assertEqual(ChecklistTemplate.objects.count(), 1)

        response = self.client.post(
            reverse(
                "helpdesk:delete_checklist_template",
                kwargs={"checklist_template_id": checklist_template.id},
            ),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_templates.html")

        self.assertEqual(ChecklistTemplate.objects.count(), 0)


class ChecklistTemplateVersioningTestCase(TicketChecklistTestCase):
    def _make_template(self, task_list, name="Onboarding"):
        return ChecklistTemplate.objects.create(name=name, task_list=task_list)

    def _make_pinned_checklist(self, template, name=None):
        """Create a checklist through the same steps the ticket view uses."""
        checklist = self.ticket.checklists.create(name=name or template.name)
        checklist.pin_template(template)
        checklist.save()
        checklist.create_tasks_from_template(template)
        return checklist

    def _migrate_url(self, checklist):
        return reverse(
            "helpdesk:migrate_ticket_checklist",
            kwargs={"ticket_id": self.ticket.id, "checklist_id": checklist.id},
        )

    def test_new_template_is_version_one_with_snapshot(self):
        template = self._make_template(["first", "second"])

        self.assertEqual(template.current_version, 1)
        self.assertEqual(template.versions.count(), 1)
        self.assertEqual(template.versions.get(version=1).task_list, ["first", "second"])

    def test_only_task_list_changes_create_new_versions(self):
        template = self._make_template(["first"])

        template.task_list = ["first", "second"]
        template.save()
        self.assertEqual(template.current_version, 2)
        self.assertEqual(template.versions.count(), 2)
        self.assertEqual(template.task_list_for_version(1), ["first"])
        self.assertEqual(template.task_list_for_version(2), ["first", "second"])

        # Renaming the template does not create another version.
        template.name = "New name"
        template.save()
        self.assertEqual(template.current_version, 2)
        self.assertEqual(template.versions.count(), 2)

    def test_checklist_pins_version_and_template_edits_do_not_touch_it(self):
        template = self._make_template(["v1 task 1", "v1 task 2", "v1 task 3"])

        # Created through the staff ticket view.
        response = self.client.post(
            reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}),
            data={"name": template.name, "checklist_template": template.id},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        old_checklist = self.ticket.checklists.get()
        self.assertEqual(old_checklist.template_id, template.id)
        self.assertEqual(old_checklist.template_version, 1)
        old_task = old_checklist.tasks.get(description="v1 task 1")
        old_task.completion_date = timezone.now()
        old_task.save(update_fields=["completion_date"])

        # Template moves to v2; the existing checklist stays on v1.
        template.task_list = ["v2 task 1", "v2 task 2"]
        template.save()

        old_checklist.refresh_from_db()
        old_task.refresh_from_db()
        self.assertEqual(old_checklist.template_version, 1)
        self.assertFalse(old_checklist.is_up_to_date)
        self.assertEqual(old_checklist.tasks.count(), 3)
        self.assertEqual(old_checklist.tasks.active().count(), 3)
        self.assertIsNotNone(old_task.completion_date)
        self.assertEqual(
            list(old_checklist.tasks.values_list("description", flat=True).order_by("position")),
            ["v1 task 1", "v1 task 2", "v1 task 3"],
        )

        # A brand new checklist gets the new version.
        new_checklist = self._make_pinned_checklist(template, name="New one")
        self.assertEqual(new_checklist.template_version, 2)
        self.assertEqual(
            list(
                new_checklist.tasks.values_list("description", flat=True).order_by(
                    "position"
                )
            ),
            ["v2 task 1", "v2 task 2"],
        )

    def test_migration_preview_lists_added_and_reset_items(self):
        template = self._make_template(
            ["kept requirement", "reworded requirement", "stable to-do"]
        )
        checklist = self._make_pinned_checklist(template)
        kept_done, reword_done, _stable = checklist.tasks.order_by("position")
        kept_done.completion_date = timezone.now()
        kept_done.save(update_fields=["completion_date"])
        reword_done.completion_date = timezone.now()
        reword_done.save(update_fields=["completion_date"])

        template.task_list = [
            "kept requirement",
            "reworded requirement (v2)",
            "stable to-do",
            "brand new task",
        ]
        template.save()

        preview = checklist.migration_preview()

        self.assertEqual(preview["source_version"], 1)
        self.assertEqual(preview["target_version"], 2)
        self.assertEqual(
            [item["description"] for item in preview["added"]], ["brand new task"]
        )
        self.assertEqual(preview["invalidated"], [])
        self.assertEqual(len(preview["reset"]), 1)
        self.assertEqual(preview["reset"][0]["task"].pk, reword_done.pk)
        self.assertEqual(preview["reset"][0]["old_description"], "reworded requirement")
        self.assertEqual(
            preview["reset"][0]["new_description"], "reworded requirement (v2)"
        )

    def test_migration_preview_lists_invalidated_items(self):
        template = self._make_template(["kept", "dropped done", "dropped to-do"])
        checklist = self._make_pinned_checklist(template)
        _kept, dropped_done, dropped_todo = checklist.tasks.order_by("position")
        dropped_done.completion_date = timezone.now()
        dropped_done.save(update_fields=["completion_date"])

        template.task_list = ["kept"]
        template.save()

        preview = checklist.migration_preview()

        self.assertEqual(preview["added"], [])
        invalidated = preview["invalidated"]
        self.assertEqual({item["task"].pk for item in invalidated}, {dropped_done.pk, dropped_todo.pk})
        # The preview reads the current state: the completed record is still there.
        self.assertIsNotNone(invalidated[0]["task"].completion_date)

    def test_migrate_preview_page_requires_confirmation(self):
        template = self._make_template(["a", "b"])
        checklist = self._make_pinned_checklist(template)
        template.task_list = ["a", "b", "c"]
        template.save()

        response = self.client.get(self._migrate_url(checklist))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "helpdesk/checklist_migrate.html")
        self.assertContains(response, "c")
        self.assertContains(response, "Confirm migration")

    def test_ticket_page_advertises_and_hides_migration_entry(self):
        template = self._make_template(["a", "b"])
        checklist = self._make_pinned_checklist(template)
        ticket_url = reverse(
            "helpdesk:view", kwargs={"ticket_id": self.ticket.id}
        )

        response = self.client.get(ticket_url)
        self.assertNotContains(response, "New version available")

        template.task_list = ["a", "b", "c"]
        template.save()

        response = self.client.get(ticket_url)
        self.assertContains(response, "New version available")
        self.assertContains(response, self._migrate_url(checklist))

        self.client.post(self._migrate_url(checklist), data={"source_version": 1})

        response = self.client.get(ticket_url)
        self.assertNotContains(response, "New version available")

    def test_migrate_apply_adds_resets_and_audits_once(self):
        template = self._make_template(
            ["kept requirement", "reworded requirement", "stable to-do"]
        )
        checklist = self._make_pinned_checklist(template)
        kept_done, reword_done, _stable = checklist.tasks.order_by("position")
        kept_done.completion_date = timezone.now()
        kept_done.save(update_fields=["completion_date"])
        reword_done.completion_date = timezone.now()
        reword_done.save(update_fields=["completion_date"])

        template.task_list = [
            "kept requirement",
            "reworded requirement (v2)",
            "stable to-do",
            "brand new task",
        ]
        template.save()

        response = self.client.post(
            self._migrate_url(checklist), data={"source_version": 1}
        )
        self.assertEqual(response.status_code, 302)

        checklist.refresh_from_db()
        self.assertEqual(checklist.template_version, 2)
        active = checklist.tasks.active().order_by("position")
        self.assertEqual(
            list(active.values_list("description", flat=True)),
            [
                "kept requirement",
                "reworded requirement (v2)",
                "stable to-do",
                "brand new task",
            ],
        )
        kept_done.refresh_from_db()
        self.assertIsNotNone(kept_done.completion_date)
        reword_done.refresh_from_db()
        self.assertEqual(reword_done.description, "reworded requirement (v2)")
        self.assertIsNone(reword_done.completion_date)
        new_task = checklist.tasks.get(description="brand new task")
        self.assertIsNone(new_task.completion_date)
        self.assertIsNone(new_task.invalidated_at)

        # Exactly one audit follow-up with one completion reset change.
        followups = self.ticket.followup_set.all()
        self.assertEqual(followups.count(), 1)
        changes = followups.get().ticketchange_set.all()
        self.assertEqual(changes.count(), 1)
        change = changes.get()
        self.assertEqual(change.old_value, "Completed")
        self.assertEqual(change.new_value, "To do")
        self.assertIn("reworded requirement", change.field)

    def test_migrate_invalidates_removed_tasks_but_keeps_their_records(self):
        template = self._make_template(["kept", "dropped done", "dropped to-do"])
        checklist = self._make_pinned_checklist(template)
        _kept, dropped_done, dropped_todo = checklist.tasks.order_by("position")
        dropped_completion = timezone.now() - timedelta(days=3)
        dropped_done.completion_date = dropped_completion
        dropped_done.save(update_fields=["completion_date"])

        template.task_list = ["kept"]
        template.save()

        response = self.client.post(
            self._migrate_url(checklist), data={"source_version": 1}, follow=True
        )
        self.assertEqual(response.status_code, 200)

        checklist.refresh_from_db()
        self.assertEqual(checklist.template_version, 2)
        self.assertEqual(checklist.tasks.active().count(), 1)
        dropped_done.refresh_from_db()
        dropped_todo.refresh_from_db()
        self.assertIsNotNone(dropped_done.invalidated_at)
        self.assertEqual(dropped_done.completion_date, dropped_completion)
        self.assertIsNotNone(dropped_todo.invalidated_at)
        # Invalidated tasks do not count towards completion anymore.
        self.assertEqual(checklist.tasks.completed().count(), 0)
        self.assertEqual(checklist.tasks.invalidated().count(), 2)

        # A follow-up update without the invalidated checkbox must not wipe
        # its historical completion record.
        response = self.client.post(
            reverse("helpdesk:update", kwargs={"ticket_id": self.ticket.id}),
            data={"comment": "updating", **self.default_update_post_data},
        )
        self.assertEqual(response.status_code, 302)
        dropped_done.refresh_from_db()
        self.assertEqual(dropped_done.completion_date, dropped_completion)

    def test_repeated_migration_submission_is_idempotent(self):
        template = self._make_template(["a", "reword me"])
        checklist = self._make_pinned_checklist(template)
        reword = checklist.tasks.get(description="reword me")
        reword.completion_date = timezone.now()
        reword.save(update_fields=["completion_date"])
        template.task_list = ["a", "reword me (v2)"]
        template.save()

        # First submission applies the migration.
        self.assertEqual(
            self.client.post(
                self._migrate_url(checklist), data={"source_version": 1}
            ).status_code,
            302,
        )
        self.assertEqual(self.ticket.followup_set.count(), 1)

        # Resubmitting the very same confirmation is rejected as stale and
        # rewrites nothing.
        response = self.client.post(
            self._migrate_url(checklist), data={"source_version": 1}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith("/migrate/"))
        self.assertEqual(self.ticket.followup_set.count(), 1)
        self.assertEqual(
            self.ticket.followup_set.get().ticketchange_set.count(), 1
        )

        # Confirming when already up-to-date is an explicit no-op as well.
        response = self.client.post(
            self._migrate_url(checklist), data={"source_version": 2}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.ticket.followup_set.count(), 1)

        # Direct API call also reports that nothing happened.
        checklist.refresh_from_db()
        self.assertFalse(checklist.migrate_to_version())

    def test_failed_migration_rolls_back_to_original_version(self):
        template = self._make_template(["a", "reword me"])
        checklist = self._make_pinned_checklist(template)
        reword = checklist.tasks.get(description="reword me")
        reword.completion_date = timezone.now()
        reword.save(update_fields=["completion_date"])
        template.task_list = ["a", "reword me (v2)", "c"]
        template.save()

        with mock.patch(
            "helpdesk.models.FollowUp.save", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    self._migrate_url(checklist), data={"source_version": 1}
                )

        checklist.refresh_from_db()
        reword.refresh_from_db()
        self.assertEqual(checklist.template_version, 1)
        self.assertEqual(checklist.tasks.active().count(), 2)
        self.assertEqual(reword.description, "reword me")
        self.assertIsNotNone(reword.completion_date)
        self.assertEqual(self.ticket.followup_set.count(), 0)
        self.assertEqual(ChecklistTask.objects.filter(description="c").count(), 0)

    def test_stale_preview_confirmation_is_rejected(self):
        template = self._make_template(["a", "b"])
        checklist = self._make_pinned_checklist(template)
        template.task_list = ["a", "b", "c"]
        template.save()

        response = self.client.post(
            self._migrate_url(checklist), data={"source_version": 99}
        )
        self.assertEqual(response.status_code, 302)
        checklist.refresh_from_db()
        self.assertEqual(checklist.template_version, 1)
        self.assertEqual(checklist.tasks.active().count(), 2)
        self.assertEqual(self.ticket.followup_set.count(), 0)

    def test_migrate_unlinked_checklist_is_not_found(self):
        checklist = self.ticket.checklists.create(name="Manual checklist")
        checklist.tasks.create(description="manual task", position=0)

        response = self.client.get(self._migrate_url(checklist))
        self.assertEqual(response.status_code, 404)

    def test_legacy_checklist_backfill_data_migration(self):
        migration = import_module(
            "helpdesk.migrations.0044_checklist_template_versioning"
        )
        template = self._make_template(["task one", "task two"])
        matching = self.ticket.checklists.create(name=template.name)
        matching.tasks.create(description="task one", position=0)
        matching.tasks.create(description="task two", position=1)
        custom = self.ticket.checklists.create(name="My own checklist")
        custom.tasks.create(description="custom task", position=0)

        # Simulate pre-deployment state: no version snapshots exist.
        ChecklistTemplateVersion.objects.all().delete()

        migration.backfill_template_versions(django_apps, None)

        matching.refresh_from_db()
        custom.refresh_from_db()
        template.refresh_from_db()
        self.assertEqual(matching.template_id, template.id)
        self.assertEqual(matching.template_version, 1)
        self.assertIsNone(custom.template)
        self.assertIsNone(custom.template_version)
        self.assertEqual(
            list(template.task_list_for_version(1)), ["task one", "task two"]
        )
