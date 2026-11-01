import io
import os
import shutil
import zipfile
from tempfile import gettempdir
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from helpdesk.models import (
    CustomField,
    FollowUp,
    FollowUpAttachment,
    Queue,
    Ticket,
    TicketCC,
    TicketDependency,
    TicketPackageImport,
)
from helpdesk.ticket_package import (
    MANIFEST_NAME,
    PAYLOAD_NAME,
    PackageError,
    export_package,
    import_package,
    load_package,
    prepare_import,
)

MEDIA_DIR = os.path.join(gettempdir(), "helpdesk_package_test_media")

User = get_user_model()


def _rebuild_zip(data, mutate=None):
    """Rewrite a package zip, applying mutate(name, bytes)->bytes."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(
        buffer, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for name in src.namelist():
            content = src.read(name)
            if mutate:
                content = mutate(name, content) or content
            dst.writestr(name, content)
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class TicketPackageTests(TestCase):
    def setUp(self):
        if os.path.isdir(MEDIA_DIR):
            shutil.rmtree(MEDIA_DIR)
        os.makedirs(MEDIA_DIR, exist_ok=True)

        self.queue = Queue.objects.create(
            title="Support",
            slug="support",
            email_address="support@example.com",
            email_box_host="mail.secret.example",
            email_box_user="mailbox-user",
            email_box_pass="s3cr3t-mail-pass",
            socks_proxy_host="10.20.30.40",
            logging_dir="/var/log/secret-helpdesk",
        )
        self.other_queue = Queue.objects.create(title="Other", slug="other")

        self.owner = User.objects.create_user(
            "owner", email="owner@example.com", password="x"
        )
        self.author = User.objects.create_user(
            "author", email="author@example.com", password="x"
        )

        self.cf = CustomField.objects.create(
            name="contract",
            label="Contract",
            data_type="varchar",
            max_length=50,
            ordering=1,
        )

        # t2 is a prerequisite of t1: it must be created first on import.
        self.t2 = Ticket.objects.create(
            queue=self.queue,
            title="Prerequisite ticket",
            description="do this first",
            status=Ticket.RESOLVED_STATUS,
            priority=2,
            submitter_email="client2@example.com",
        )
        self.t1 = Ticket.objects.create(
            queue=self.queue,
            title="Main ticket",
            description="needs the prerequisite",
            priority=1,
            assigned_to=self.owner,
            submitter_email="client@example.com",
        )
        self.t1.secret_key = "super-secret-view-key-123456"
        self.t1.save()
        TicketDependency.objects.create(ticket=self.t1, depends_on=self.t2)

        self.t1.ticketcustomfieldvalue_set.create(field=self.cf, value="CTR-42")
        TicketCC.objects.create(
            ticket=self.t1, email="follower@example.com", can_view=True
        )
        TicketCC.objects.create(ticket=self.t1, user=self.author)

        fup = FollowUp.objects.create(
            ticket=self.t1,
            title="Initial reply",
            comment="public comment",
            public=True,
            user=self.author,
            message_id="<secret-message-id@mail.example>",
        )
        fup.ticketchange_set.create(field="Priority", old_value="3", new_value="1")
        att = FollowUpAttachment(
            followup=fup, filename="note.txt", mime_type="text/plain", size=12
        )
        att.file.save("note.txt", ContentFile(b"hello attach"), save=True)
        FollowUp.objects.create(
            ticket=self.t1,
            title="Internal note",
            comment="private",
            public=False,
            email_recipients=["notified@example.com"],
        )

    def tearDown(self):
        if os.path.isdir(MEDIA_DIR):
            shutil.rmtree(MEDIA_DIR, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def export(self, tickets=None):
        tickets = tickets if tickets is not None else Ticket.objects.all()
        return export_package(tickets.order_by("id"))

    def test_round_trip_preserves_content_relations_and_attachments(self):
        data = self.export()
        package = load_package(data)
        plan = prepare_import(package)
        self.assertTrue(plan.ready, plan.errors)
        created = import_package(package, plan)

        self.assertEqual(len(created), 2)
        self.assertEqual(Ticket.objects.count(), 4)
        self.assertEqual(len(mail.outbox), 0)

        new_main = Ticket.objects.exclude(id__in=[self.t1.id, self.t2.id]).get(
            title="Main ticket"
        )
        new_pre = Ticket.objects.exclude(id__in=[self.t1.id, self.t2.id]).get(
            title="Prerequisite ticket"
        )

        # Fresh tickets, new secret keys, values preserved
        self.assertNotEqual(new_main.id, self.t1.id)
        self.assertNotEqual(new_main.secret_key, "super-secret-view-key-123456")
        self.assertEqual(new_main.description, "needs the prerequisite")
        self.assertEqual(new_main.priority, 1)
        self.assertEqual(new_main.assigned_to, self.owner)
        self.assertEqual(new_pre.status, Ticket.RESOLVED_STATUS)
        self.assertEqual(new_main.created, self.t1.created)

        # Dependency order: prerequisite created before the dependent
        self.assertLess(new_pre.id, new_main.id)
        dep = TicketDependency.objects.get(ticket=new_main)
        self.assertEqual(dep.depends_on, new_pre)

        # Followers
        emails = {
            cc.email_address for cc in new_main.ticketcc_set.select_related("user")
        }
        self.assertIn("follower@example.com", emails)
        self.assertIn("author@example.com", emails)

        # Custom field
        self.assertEqual(
            new_main.ticketcustomfieldvalue_set.get(field=self.cf).value, "CTR-42"
        )

        # Follow-ups, changes and attachment content
        new_fup = new_main.followup_set.get(comment="public comment")
        self.assertTrue(new_fup.public)
        self.assertEqual(new_fup.user, self.author)
        self.assertIsNone(new_fup.message_id)
        self.assertIsNone(new_fup.email_recipients)
        self.assertEqual(new_fup.ticketchange_set.get().new_value, "1")
        new_att = new_fup.followupattachment_set.get()
        with new_att.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"hello attach")
        self.assertTrue(
            FollowUpAttachment.objects.filter(
                followup__ticket=new_main
            ).count(),
            1,
        )

        # Idempotency records exist
        refs = set(
            TicketPackageImport.objects.filter(
                package_id=package.package_id
            ).values_list("local_ref", flat=True)
        )
        self.assertEqual(refs, {str(self.t1.id), str(self.t2.id)})

    def test_package_excludes_credentials_hosts_and_secret_keys(self):
        data = self.export()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            payload = archive.read(PAYLOAD_NAME)
            manifest = archive.read(MANIFEST_NAME)
        blob = payload + manifest
        for secret in (
            b"s3cr3t-mail-pass",
            b"mailbox-user",
            b"mail.secret.example",
            b"10.20.30.40",
            b"/var/log/secret-helpdesk",
            b"super-secret-view-key-123456",
            b"secret-message-id",
            b"notified@example.com",
        ):
            self.assertNotIn(secret, blob)
        # Only whitelisted queue identity is present, never its mailbox config.
        self.assertIn(b"support", payload)

    def test_tampered_payload_is_rejected(self):
        data = self.export()
        tampered = _rebuild_zip(
            data,
            mutate=lambda name, content: content.replace(
                b"Main ticket", b"Pwned ticket"
            )
            if name == PAYLOAD_NAME
            else content,
        )
        with self.assertRaises(PackageError):
            load_package(tampered)

    def test_tampered_attachment_is_rejected(self):
        data = self.export()
        tampered = _rebuild_zip(
            data,
            mutate=lambda name, content: b"x" + content
            if name.startswith("attachments/")
            else content,
        )
        with self.assertRaises(PackageError):
            load_package(tampered)

    def test_zip_slip_and_garbage_are_rejected(self):
        with self.assertRaises(PackageError):
            load_package(b"this is not a zip at all")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("../evil.txt", b"x")
        with self.assertRaises(PackageError):
            load_package(buffer.getvalue())

    def test_missing_queue_blocks_and_queue_remap_resolves(self):
        data = self.export()
        # Simulate a target environment without the source queue.
        Ticket.objects.all().delete()
        self.queue.delete()

        package = load_package(data)
        plan = prepare_import(package)
        self.assertFalse(plan.ready)
        self.assertIn("support", plan.missing_queue_slugs)

        plan = prepare_import(package, queue_map={"support": self.other_queue.id})
        self.assertTrue(plan.ready, plan.errors)
        created = import_package(package, plan)
        self.assertEqual({t.queue.slug for t in created}, {"other"})

    def test_missing_users_become_empty_refs_and_email_followers(self):
        data = self.export()
        Ticket.objects.all().delete()
        self.owner.delete()
        self.author.delete()

        package = load_package(data)
        plan = prepare_import(package)
        self.assertTrue(plan.ready)
        self.assertTrue(plan.warnings)  # unknown users reported
        created = import_package(package, plan)
        new_main = next(t for t in created if t.title == "Main ticket")
        self.assertIsNone(new_main.assigned_to)
        followup = new_main.followup_set.get(comment="public comment")
        self.assertIsNone(followup.user)
        # User-based follower falls back to its stored e-mail address.
        self.assertTrue(
            new_main.ticketcc_set.filter(email="author@example.com").exists()
        )

    def test_missing_custom_field_is_skipped_with_warning(self):
        data = self.export()
        Ticket.objects.all().delete()
        CustomField.objects.all().delete()

        package = load_package(data)
        plan = prepare_import(package)
        self.assertTrue(plan.ready)
        self.assertTrue(any("contract" in w for w in plan.warnings))
        import_package(package, plan)
        self.assertFalse(
            Ticket.objects.filter(title="Main ticket")
            .first()
            .ticketcustomfieldvalue_set.exists()
        )

    def test_external_dependency_blocks_then_can_be_dropped(self):
        external = Ticket.objects.create(
            queue=self.queue, title="Not exported", description="outside package"
        )
        child = Ticket.objects.create(
            queue=self.queue, title="Depends on outsider"
        )
        TicketDependency.objects.create(ticket=child, depends_on=external)
        data = export_package(Ticket.objects.filter(id=child.id))

        package = load_package(data)
        plan = prepare_import(package)
        self.assertFalse(plan.ready)
        self.assertTrue(plan.missing_references)
        with self.assertRaises(PackageError):
            import_package(package, plan)

        plan = prepare_import(package, drop_external_refs=True)
        self.assertTrue(plan.ready)
        imported = import_package(package, plan)
        self.assertFalse(TicketDependency.objects.filter(ticket=imported[0]).exists())

    def test_failure_midway_rolls_back_everything_including_files(self):
        data = self.export()
        before = Ticket.objects.count()

        package = load_package(data)
        plan = prepare_import(package)
        with mock.patch(
            "helpdesk.ticket_package.TicketDependency.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                import_package(package, plan)

        self.assertEqual(Ticket.objects.count(), before)
        self.assertEqual(
            TicketPackageImport.objects.filter(package_id=package.package_id).count(),
            0,
        )
        # No orphan attachment files left behind by the aborted batch.
        written = []
        for _root, _dirs, files in os.walk(MEDIA_DIR):
            written.extend(files)
        # only the source attachment remains (1); nothing from the import
        self.assertEqual(written.count("note.txt"), 1)

    def test_reimporting_same_package_is_idempotent(self):
        data = self.export()
        package = load_package(data)
        import_package(package, prepare_import(package))
        self.assertEqual(Ticket.objects.count(), 4)

        # Second plan detects the previous import and import is a no-op.
        plan2 = prepare_import(package)
        self.assertEqual(len(plan2.already_imported), 2)
        again = import_package(package, plan2)
        self.assertEqual(len(again), 2)
        self.assertEqual(Ticket.objects.count(), 4)
        self.assertEqual(
            TicketPackageImport.objects.filter(package_id=package.package_id).count(),
            2,
        )


@override_settings(MEDIA_ROOT=MEDIA_DIR)
class TicketPackageViewTests(TestCase):
    def setUp(self):
        if os.path.isdir(MEDIA_DIR):
            shutil.rmtree(MEDIA_DIR)
        os.makedirs(MEDIA_DIR, exist_ok=True)
        self.staff = User.objects.create_superuser(
            "root", email="root@example.com", password="pass"
        )
        self.client.login(username="root", password="pass")
        self.queue = Queue.objects.create(title="Support", slug="support")
        self.ticket = Ticket.objects.create(
            queue=self.queue, title="Export me", description="body"
        )

    def tearDown(self):
        if os.path.isdir(MEDIA_DIR):
            shutil.rmtree(MEDIA_DIR, ignore_errors=True)

    def test_requires_staff(self):
        self.client.logout()
        response = self.client.get(reverse("helpdesk:package_import"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_export_through_mass_update(self):
        response = self.client.post(
            reverse("helpdesk:mass_update"),
            {"ticket_id": [self.ticket.id], "action": "export_package"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn("attachment", response["Content-Disposition"])
        package = load_package(response.content)
        self.assertEqual(len(package.tickets), 1)

    def test_preview_confirm_and_repeat_import_flow(self):
        data = export_package(Ticket.objects.filter(id=self.ticket.id))
        upload = SimpleUploadedFile(
            "pkg.zip", data, content_type="application/zip"
        )
        response = self.client.post(
            reverse("helpdesk:package_import"),
            {"stage": "preview", "package": upload},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checksums verified")
        self.assertContains(response, "Export me")

        response = self.client.post(
            reverse("helpdesk:package_import"), {"stage": "confirm"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Ticket.objects.filter(title="Export me").count(), 2
        )

        # Repeat: preview marks already imported, confirm creates no duplicates
        upload2 = SimpleUploadedFile(
            "pkg.zip", data, content_type="application/zip"
        )
        response = self.client.post(
            reverse("helpdesk:package_import"),
            {"stage": "preview", "package": upload2},
        )
        self.assertContains(response, "already been imported")
        response = self.client.post(
            reverse("helpdesk:package_import"), {"stage": "confirm"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Ticket.objects.filter(title="Export me").count(), 2
        )

    def test_preview_rejects_bad_package(self):
        upload = SimpleUploadedFile(
            "bad.zip", b"garbage", content_type="application/zip"
        )
        response = self.client.post(
            reverse("helpdesk:package_import"),
            {"stage": "preview", "package": upload},
            follow=True,
        )
        self.assertContains(response, "Not a valid ZIP archive")
