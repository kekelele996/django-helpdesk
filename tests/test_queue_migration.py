import os
from tempfile import gettempdir
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from helpdesk import queue_migration as qm
from helpdesk import settings as helpdesk_settings
from helpdesk.models import (
    Checklist,
    CustomField,
    FollowUp,
    FollowUpAttachment,
    Queue,
    QueueMigration,
    QueueMigrationItem,
    Ticket,
    TicketCC,
    TicketChange,
    TicketCustomFieldValue,
    TicketDependency,
)
from helpdesk.queue_migration import (
    MigrationConflict,
    MigrationPermissionError,
    QueueMigrationError,
    build_preview_data,
    create_preview,
    perform_migration,
)


User = get_user_model()

MEDIA_DIR = os.path.join(gettempdir(), "helpdesk_test_migration_media")


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class QueueMigrationServiceTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff", password="password", is_staff=True
        )
        self.q1 = Queue.objects.create(title="Support", slug="support")
        self.q2 = Queue.objects.create(title="Escalations", slug="escalations")

        self.t1 = Ticket.objects.create(title="First ticket", queue=self.q1)
        self.t2 = Ticket.objects.create(
            title="Second ticket", queue=self.q1, assigned_to=self.staff
        )
        # Historical reply that must remain untouched.
        self.history = FollowUp.objects.create(
            ticket=self.t1, title="Old reply", comment="history body", public=True
        )
        self.attachment = FollowUpAttachment.objects.create(
            followup=self.history,
            file=SimpleUploadedFile("note.txt", b"attachment bytes", "text/plain"),
        )

        self.cf = CustomField.objects.create(
            name="contract", label="Contract", data_type="varchar", max_length=100
        )
        TicketCustomFieldValue.objects.create(
            ticket=self.t1, field=self.cf, value="ACME"
        )

        TicketCC.objects.create(ticket=self.t1, email="external@example.com")
        TicketDependency.objects.create(ticket=self.t1, depends_on=self.t2)
        Checklist.objects.create(ticket=self.t1, name="Onboarding")

    def _preview(self, tickets=None, target=None, reason=""):
        return create_preview(
            operator=self.staff,
            tickets=tickets or [self.t1, self.t2],
            target_queue=target or self.q2,
            reason=reason,
        )

    def test_preview_lists_field_mapping_and_counts(self):
        data = build_preview_data(self._preview())

        self.assertEqual(data["totals"]["tickets"], 2)
        self.assertEqual(data["totals"]["moving"], 2)
        self.assertEqual(data["totals"]["already_in_target"], 0)
        self.assertEqual(data["totals"]["attachments"], 1)

        fields = [row["field"] for row in data["field_mapping_rows"]]
        self.assertTrue(any("Queue" in f for f in fields))
        treatments = " ".join(row["treatment"] for row in data["field_mapping_rows"])
        self.assertIn(self.q2.title, treatments)

        self.assertEqual(list(data["custom_fields"]), [self.cf])

        rows = {row["ticket"].id: row for row in data["item_rows"]}
        row1 = rows[self.t1.id]
        self.assertEqual(row1["counts"]["attachments"], 1)
        self.assertEqual(row1["counts"]["cc_emails"], 1)
        self.assertEqual(row1["counts"]["custom_values"], 1)
        self.assertEqual(row1["counts"]["dependencies"], 1)
        self.assertEqual(row1["counts"]["checklists"], 1)
        self.assertFalse(row1["already_in_target"])

    def test_perform_moves_batch_and_preserves_all_related_data(self):
        migration = self._preview(reason="Team reorganisation")
        stored_path = self.attachment.file.name
        original_followup_ids = set(
            FollowUp.objects.values_list("id", flat=True)
        )

        summary = perform_migration(self.staff, migration)

        self.assertEqual(len(summary["migrated"]), 2)
        self.assertEqual(len(summary["unchanged"]), 0)

        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q2.id)
        self.assertEqual(self.t2.queue_id, self.q2.id)
        self.assertEqual(self.t2.assigned_to, self.staff)

        new_followups = FollowUp.objects.exclude(id__in=original_followup_ids)
        self.assertEqual(new_followups.count(), 2)
        for followup in new_followups:
            self.assertFalse(followup.public)
            self.assertEqual(followup.email_recipients, [])
            self.assertEqual(followup.comment, "Team reorganisation")
            change = followup.ticketchange_set.get()
            self.assertEqual(change.field, "Queue")
            self.assertEqual(change.old_value, str(self.q1.id))
            self.assertEqual(change.new_value, str(self.q2.id))
        # Related data survives the move untouched.
        self.attachment.refresh_from_db()
        self.assertEqual(self.attachment.file.name, stored_path)
        self.attachment.file.open("rb")
        try:
            self.assertEqual(self.attachment.file.read(), b"attachment bytes")
        finally:
            self.attachment.file.close()

        self.history.refresh_from_db()
        self.assertEqual(self.history.comment, "history body")
        self.assertEqual(
            TicketCC.objects.get(ticket=self.t1).email_address,
            "external@example.com",
        )
        self.assertEqual(
            TicketCustomFieldValue.objects.get(ticket=self.t1).value, "ACME"
        )
        self.assertTrue(
            TicketDependency.objects.filter(ticket=self.t1, depends_on=self.t2)
        )
        self.assertTrue(Checklist.objects.filter(ticket=self.t1).exists())

        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.COMPLETED)
        self.assertIsNotNone(migration.confirmed_at)
        self.assertTrue(
            QueueMigrationItem.objects.filter(
                migration=migration, status=QueueMigrationItem.MIGRATED
            ).count(),
            2,
        )

    def test_failure_rolls_back_the_whole_batch(self):
        migration = self._preview()
        real_save = FollowUp.save
        calls = {"n": 0}

        def failing_save(followup, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure")
            return real_save(followup, *args, **kwargs)

        with mock.patch.object(FollowUp, "save", failing_save):
            with self.assertRaises(QueueMigrationError) as raised:
                perform_migration(self.staff, migration)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q1.id)
        self.assertEqual(self.t2.queue_id, self.q1.id)
        self.assertFalse(
            FollowUp.objects.filter(title__contains="batch migration").exists()
        )
        self.assertFalse(
            TicketChange.objects.filter(field="Queue").exists()
        )
        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.PREVIEWED)
        self.assertFalse(
            QueueMigrationItem.objects.exclude(
                status=QueueMigrationItem.PENDING
            ).exists()
        )

    def test_repeating_same_batch_creates_no_duplicate_changes(self):
        migration = self._preview()
        perform_migration(self.staff, migration)
        followups_after_first = set(
            FollowUp.objects.values_list("id", flat=True)
        )
        # Re-confirming the completed batch is a no-op.
        summary = perform_migration(self.staff, migration)
        self.assertEqual(len(summary["migrated"]), 2)
        self.assertFalse(
            FollowUp.objects.exclude(id__in=followups_after_first).exists()
        )
        # A new batch over tickets already in the target writes nothing.
        second = self._preview()
        data = build_preview_data(second)
        self.assertEqual(data["totals"]["moving"], 0)
        self.assertEqual(data["totals"]["already_in_target"], 2)
        second_summary = perform_migration(self.staff, second)
        self.assertEqual(second_summary["migrated"], [])
        self.assertEqual(len(second_summary["unchanged"]), 2)
        self.assertFalse(
            FollowUp.objects.exclude(id__in=followups_after_first).exists()
        )

    def test_ticket_moved_after_preview_aborts_everything(self):
        q3 = Queue.objects.create(title="Sales", slug="sales")
        migration = self._preview()
        # Someone else moves one ticket after the preview was generated.
        self.t1.queue = q3
        self.t1.save()

        with self.assertRaises(MigrationConflict):
            perform_migration(self.staff, migration)

        self.t2.refresh_from_db()
        self.assertEqual(self.t2.queue_id, self.q1.id)
        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.PREVIEWED)

    def test_ticket_already_in_target_is_left_untouched(self):
        t3 = Ticket.objects.create(title="Already there", queue=self.q2)
        migration = create_preview(
            operator=self.staff,
            tickets=[self.t1, t3],
            target_queue=self.q2,
        )
        before = set(FollowUp.objects.values_list("id", flat=True))

        summary = perform_migration(self.staff, migration)

        self.assertEqual(len(summary["migrated"]), 1)
        self.assertEqual(len(summary["unchanged"]), 1)
        self.t1.refresh_from_db()
        t3.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q2.id)
        self.assertEqual(t3.queue_id, self.q2.id)
        self.assertEqual(set(FollowUp.objects.values_list("id", flat=True)) - before,
                         {summary["migrated"][0].followup_id})

    def test_cancel_preview_does_not_touch_tickets(self):
        migration = self._preview()
        qm.cancel_preview(self.staff, migration)
        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.CANCELLED)
        with self.assertRaises(QueueMigrationError):
            perform_migration(self.staff, migration)
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q1.id)

    def test_permissions_are_not_modified(self):
        perm_ids_before = set(Permission.objects.values_list("id", flat=True))
        staff_perms_before = set(
            self.staff.user_permissions.values_list("id", flat=True)
        )

        perform_migration(self.staff, self._preview())

        self.assertEqual(
            set(Permission.objects.values_list("id", flat=True)), perm_ids_before
        )
        self.assertEqual(
            set(self.staff.user_permissions.values_list("id", flat=True)),
            staff_perms_before,
        )

    def test_non_staff_operator_rejected(self):
        plain = User.objects.create_user(username="plain", password="password")
        with self.assertRaises(MigrationPermissionError):
            create_preview(
                operator=plain, tickets=[self.t1], target_queue=self.q2
            )


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class QueueMigrationPerQueuePermissionTests(TestCase):
    def setUp(self):
        self._original = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.superuser = User.objects.create_superuser(
            username="root", password="password", email="root@example.com"
        )
        self.q1 = Queue.objects.create(title="Q1", slug="q1")
        self.q2 = Queue.objects.create(title="Q2", slug="q2")

        self.user_q1 = self._user_with_queue("u_q1", self.q1)
        self.user_q2 = self._user_with_queue("u_q2", self.q2)
        # Queue access granted through a group must be recognised too.
        self.group_user = User.objects.create_user(
            username="u_group", password="password", is_staff=True
        )
        group = Group.objects.create(name="q2-group")
        group.permissions.add(self._perm(self.q2))
        self.group_user.groups.add(group)

        self.follower = self._user_with_queue("follower", self.q1)
        self.ticket = Ticket.objects.create(
            title="Guarded ticket",
            queue=self.q1,
            assigned_to=self.user_q1,
        )
        TicketCC.objects.create(ticket=self.ticket, user=self.follower)
        TicketCC.objects.create(ticket=self.ticket, email="external@example.com")

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self._original

    @staticmethod
    def _perm(queue):
        return Permission.objects.get(codename=queue.permission_name[9:])

    def _user_with_queue(self, username, queue):
        user = User.objects.create_user(
            username=username, password="password", is_staff=True,
            email=f"{username}@example.com",
        )
        user.user_permissions.add(self._perm(queue))
        return user

    def test_operator_without_target_queue_permission_rejected(self):
        with self.assertRaises(MigrationPermissionError):
            create_preview(
                operator=self.user_q1,
                tickets=[self.ticket],
                target_queue=self.q2,
            )

    def test_preview_flags_recipients_and_losing_followers(self):
        migration = create_preview(
            operator=self.superuser,
            tickets=[self.ticket],
            target_queue=self.q2,
        )
        data = build_preview_data(migration)

        new_names = {u.username for u in data["recipients_new"]}
        self.assertEqual(new_names, {"u_q2", "u_group"})
        existing_names = {u.username for u in data["recipients_existing"]}
        self.assertIn("root", existing_names)
        self.assertNotIn("u_q1", existing_names)

        self.assertEqual(
            [u.username for u in data["losing_followers"]], ["follower"]
        )
        self.assertEqual(
            [u.username for u in data["losing_owners"]], ["u_q1"]
        )
        # E-mail-only followers are never flagged for staff access loss.
        row = data["item_rows"][0]
        self.assertEqual([u.username for u in row["losing_cc_users"]], ["follower"])
        self.assertEqual(row["cc_emails"], ["external@example.com"])

    def test_cc_records_survive_move(self):
        migration = create_preview(
            operator=self.superuser,
            tickets=[self.ticket],
            target_queue=self.q2,
        )
        perform_migration(self.superuser, migration)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.queue_id, self.q2.id)
        cc_users = list(
            TicketCC.objects.filter(ticket=self.ticket).values_list(
                "user__username", "email"
            )
        )
        self.assertIn(("follower", None), cc_users)
        self.assertIn((None, "external@example.com"), cc_users)


class QueueMigrationViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="staff", password="password", is_staff=True
        )
        self.other = User.objects.create_user(
            username="other", password="password", is_staff=True
        )
        self.client.force_login(self.staff)

        self.q1 = Queue.objects.create(title="Support", slug="support")
        self.q2 = Queue.objects.create(title="Escalations", slug="escalations")
        self.t1 = Ticket.objects.create(title="One", queue=self.q1)
        self.t2 = Ticket.objects.create(title="Two", queue=self.q1)

    def test_create_view_get_prefills_selected_tickets(self):
        response = self.client.get(
            reverse("helpdesk:queue_migration_create")
            + f"?tickets={self.t1.id}&tickets={self.t2.id}"
        )
        self.assertEqual(response.status_code, 200)
        values = [
            int(str(choice[0]))
            for choice in response.context["form"].fields["tickets"].choices
            if choice[0]
        ]
        self.assertIn(self.t1.id, values)
        self.assertIn(self.t2.id, values)

    def test_full_preview_confirm_flow(self):
        response = self.client.post(
            reverse("helpdesk:queue_migration_create"),
            {
                "tickets": [self.t1.id, self.t2.id],
                "target_queue": self.q2.id,
                "reason": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        migration = QueueMigration.objects.get()
        self.assertEqual(migration.status, QueueMigration.PREVIEWED)
        detail_url = reverse(
            "helpdesk:queue_migration_detail", args=[migration.id]
        )
        self.assertEqual(response["Location"], detail_url)

        preview = self.client.get(detail_url)
        self.assertEqual(preview.status_code, 200)
        self.assertContains(preview, "Field mapping")

        confirmed = self.client.post(detail_url, {"action": "confirm"})
        self.assertEqual(confirmed.status_code, 200)
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q2.id)
        self.assertEqual(self.t2.queue_id, self.q2.id)
        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.COMPLETED)

    def test_cancel_flow(self):
        migration = create_preview(
            operator=self.staff, tickets=[self.t1], target_queue=self.q2
        )
        response = self.client.post(
            reverse("helpdesk:queue_migration_detail", args=[migration.id]),
            {"action": "cancel"},
        )
        self.assertEqual(response.status_code, 302)
        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.CANCELLED)
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q1.id)

    def test_failed_confirmation_rolls_back_and_reports_error(self):
        migration = create_preview(
            operator=self.staff, tickets=[self.t1, self.t2], target_queue=self.q2
        )

        def failing_save(followup, *args, **kwargs):
            raise RuntimeError("boom")

        with mock.patch.object(FollowUp, "save", failing_save):
            response = self.client.post(
                reverse("helpdesk:queue_migration_detail", args=[migration.id]),
                {"action": "confirm"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rolled back")
        self.t1.refresh_from_db()
        self.t2.refresh_from_db()
        self.assertEqual(self.t1.queue_id, self.q1.id)
        self.assertEqual(self.t2.queue_id, self.q1.id)
        migration.refresh_from_db()
        self.assertEqual(migration.status, QueueMigration.PREVIEWED)

    def test_detail_forbidden_for_other_user(self):
        migration = create_preview(
            operator=self.staff, tickets=[self.t1], target_queue=self.q2
        )
        self.client.force_login(self.other)
        response = self.client.get(
            reverse("helpdesk:queue_migration_detail", args=[migration.id])
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("helpdesk:queue_migration_create"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_mass_update_action_redirects_to_migration_wizard(self):
        response = self.client.post(
            reverse("helpdesk:mass_update"),
            {
                "ticket_id": [self.t1.id, self.t2.id],
                "action": "migrate_queue",
            },
        )
        self.assertEqual(response.status_code, 302)
        location = response["Location"]
        self.assertIn(reverse("helpdesk:queue_migration_create"), location)
        self.assertIn(f"tickets={self.t1.id}", location)
        self.assertIn(f"tickets={self.t2.id}", location)
