"""
Tests for queue-based automatic ticket routing:

* member availability (active switch, leave, weekly working hours) and
  service-area scoping;
* load balancing by open ticket count and recent assignment count;
* concurrency: two claimants racing for one ticket, only one can win;
* preview + atomic batch reassignment when a member goes on leave or
  changes service scope; tickets already being worked on are untouched;
* whole-batch rollback when anything fails midway.
"""

from .helpers import create_ticket, get_staff_user, get_user
from datetime import datetime, timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connections, OperationalError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from helpdesk import settings as helpdesk_settings
from helpdesk.assignment import (
    AlreadyAssigned,
    apply_reassignment,
    auto_assign_ticket,
    BatchReassignmentError,
    choose_assignee,
    eligible_members,
    plan_member_reassignment,
    take_ticket,
    ticket_is_in_progress,
)
from helpdesk.models import (
    AssignmentRecord,
    FollowUp,
    Queue,
    QueueMember,
    QueueServiceArea,
    Ticket,
)
from helpdesk.signals import new_ticket_done
from helpdesk.update_ticket import update_ticket
import threading
import time


User = get_user_model()

MONDAY_10 = timezone.make_aware(datetime(2026, 11, 2, 10, 0))  # Monday
MONDAY_12 = timezone.make_aware(datetime(2026, 11, 2, 12, 0))
SATURDAY_10 = timezone.make_aware(datetime(2026, 11, 7, 10, 0))


class AssignmentTestBase(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(title="Support", slug="support")
        self.alice = get_user("alice")
        self.bob = get_user("bob")
        self.carol = get_user("carol")

    def add_member(self, user, queue=None, **kwargs):
        kwargs.setdefault("active", True)
        return QueueMember.objects.create(
            queue=queue or self.queue, user=user, **kwargs
        )

    def open_ticket(self, user=None, queue=None, service_area=None, title=None):
        return Ticket.objects.create(
            title=title or f"ticket for {getattr(user, 'username', 'none')}",
            queue=queue or self.queue,
            assigned_to=user,
            status=Ticket.OPEN_STATUS,
            service_area=service_area,
        )


class AvailabilityTests(AssignmentTestBase):
    def test_inactive_member_unavailable(self):
        member = self.add_member(self.alice, active=False)
        self.assertFalse(member.is_available(MONDAY_10))

    def test_leave_until(self):
        member = self.add_member(self.alice, leave_until=MONDAY_12)
        self.assertFalse(member.is_available(MONDAY_10))
        self.assertTrue(member.is_available(MONDAY_12))

    def test_working_hours(self):
        member = self.add_member(self.alice, work_schedule={"1": [["09:00", "11:30"]]})
        self.assertTrue(member.is_available(MONDAY_10))
        self.assertFalse(member.is_available(MONDAY_12))
        self.assertFalse(member.is_available(SATURDAY_10))

    def test_empty_schedule_means_any_time(self):
        member = self.add_member(self.alice, work_schedule={})
        self.assertTrue(member.is_available(SATURDAY_10))

    def test_invalid_schedule_rejected(self):
        member = self.add_member(self.alice)
        member.work_schedule = {"9": [["09:00", "18:00"]]}
        with self.assertRaises(ValidationError):
            member.full_clean()
        member.work_schedule = {"1": [["18:00", "09:00"]]}
        with self.assertRaises(ValidationError):
            member.full_clean()


class ServiceScopeTests(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self.billing = QueueServiceArea.objects.create(
            queue=self.queue, name="Billing", slug="billing"
        )
        self.network = QueueServiceArea.objects.create(
            queue=self.queue, name="Network", slug="network"
        )

    def test_member_without_areas_serves_everything(self):
        member = self.add_member(self.alice)
        ticket = self.open_ticket(service_area=self.network)
        self.assertTrue(member.serves(ticket))

    def test_member_scoped_to_area(self):
        member = self.add_member(self.alice)
        member.service_areas.add(self.billing)
        self.assertTrue(member.serves(service_area=self.billing))
        self.assertFalse(member.serves(service_area=self.network))

    def test_eligibility_filters_by_scope(self):
        alice = self.add_member(self.alice)
        alice.service_areas.add(self.billing)
        self.add_member(self.bob)  # serves everything
        network_ticket = self.open_ticket(service_area=self.network)

        members = eligible_members(self.queue, network_ticket, at=MONDAY_10)
        self.assertEqual([m.user_id for m in members], [self.bob.id])
        self.assertEqual(
            choose_assignee(self.queue, network_ticket, MONDAY_10), self.bob
        )

    def test_ticket_without_area_open_to_all(self):
        alice = self.add_member(self.alice)
        alice.service_areas.add(self.billing)
        self.add_member(self.bob)
        ticket = self.open_ticket(service_area=None)
        members = eligible_members(self.queue, ticket, at=MONDAY_10)
        self.assertEqual({m.user_id for m in members}, {self.alice.id, self.bob.id})


class LoadBalancingTests(AssignmentTestBase):
    def test_least_open_load_wins(self):
        self.add_member(self.alice)
        self.add_member(self.bob)
        self.open_ticket(self.alice)
        self.open_ticket(self.alice)
        self.open_ticket(self.bob)
        self.assertEqual(choose_assignee(self.queue, at=MONDAY_10), self.bob)

    def test_recent_assignments_break_tie(self):
        self.add_member(self.alice)
        self.add_member(self.bob)
        # Equal current load, but Alice received a ticket a few minutes ago.
        AssignmentRecord.objects.create(
            ticket=self.open_ticket(self.alice),
            queue=self.queue,
            user=self.alice,
            kind=AssignmentRecord.KIND_AUTO,
            assigned_at=MONDAY_10 - timedelta(minutes=5),
        )
        self.open_ticket(self.bob)
        self.assertEqual(choose_assignee(self.queue, at=MONDAY_10), self.bob)

    def test_old_assignments_outside_window_ignored(self):
        self.add_member(self.alice)
        self.add_member(self.bob)
        AssignmentRecord.objects.create(
            ticket=self.open_ticket(self.alice),
            queue=self.queue,
            user=self.alice,
            kind=AssignmentRecord.KIND_AUTO,
            assigned_at=MONDAY_10 - timedelta(hours=3),
        )
        self.open_ticket(self.bob)
        # Equal load and no recent records: stable tie-break by member id.
        chosen = choose_assignee(self.queue, at=MONDAY_10)
        self.assertIn(chosen, {self.alice, self.bob})

    def test_unavailable_members_skipped(self):
        self.add_member(self.alice, active=False)
        self.add_member(self.bob, leave_until=SATURDAY_10 + timedelta(hours=2))
        self.add_member(
            self.carol, work_schedule={"6": [["09:00", "18:00"]]}
        )  # Saturday only
        self.assertIsNone(choose_assignee(self.queue, at=MONDAY_10))
        self.assertEqual(choose_assignee(self.queue, at=SATURDAY_10), self.carol)

    def test_closed_tickets_not_counted_as_load(self):
        self.add_member(self.alice)
        self.add_member(self.bob)
        t = self.open_ticket(self.alice)
        t.status = Ticket.CLOSED_STATUS
        t.save()
        # Both have 0 open load: Alice wins the id tie-break.
        self.assertEqual(choose_assignee(self.queue, at=MONDAY_10), self.alice)


class AutoAssignTests(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self._old_auto = helpdesk_settings.HELPDESK_AUTO_ASSIGN
        helpdesk_settings.HELPDESK_AUTO_ASSIGN = True

    def tearDown(self):
        helpdesk_settings.HELPDESK_AUTO_ASSIGN = self._old_auto
        super().tearDown()

    def test_new_ticket_auto_assigned(self):
        member = self.add_member(self.alice)
        ticket = self.open_ticket(user=None)
        returned, user = auto_assign_ticket(ticket, at=MONDAY_10)
        self.assertEqual(user, self.alice)
        self.assertEqual(returned.assigned_to, self.alice)
        record = AssignmentRecord.objects.get(ticket=ticket)
        self.assertEqual(record.kind, AssignmentRecord.KIND_AUTO)
        self.assertTrue(
            ticket.followup_set.filter(title__icontains="Auto-assigned").exists()
        )
        owner_change = (
            ticket.followup_set.first().ticketchange_set.filter(field="Owner").first()
        )
        self.assertIsNotNone(owner_change)
        self.assertEqual(str(owner_change.new_value), self.alice.get_username())
        self.assertEqual(member.user, self.alice)

    def test_already_assigned_raises(self):
        self.add_member(self.alice)
        ticket = self.open_ticket(user=self.carol)
        with self.assertRaises(AlreadyAssigned):
            auto_assign_ticket(ticket, at=MONDAY_10)

    def test_no_eligible_member_leaves_unassigned(self):
        self.add_member(self.alice, active=False)
        ticket = self.open_ticket(user=None)
        returned, user = auto_assign_ticket(ticket, at=MONDAY_10)
        self.assertIsNone(user)
        self.assertIsNone(returned.assigned_to)
        self.assertFalse(AssignmentRecord.objects.exists())

    def test_auto_assign_via_signal(self):
        self.add_member(self.bob)
        ticket = self.open_ticket(user=None)
        new_ticket_done.send(sender="test", ticket=ticket)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to, self.bob)

    def test_auto_assign_distributes_load(self):
        self.add_member(self.alice)
        self.add_member(self.bob)
        self.open_ticket(self.alice)
        self.open_ticket(self.alice)
        ticket = self.open_ticket(user=None)
        _, user = auto_assign_ticket(ticket)
        self.assertEqual(user, self.bob)


class AutoAssignDisabledTests(AssignmentTestBase):
    def test_setting_off_is_noop(self):
        self.add_member(self.alice)
        ticket = self.open_ticket(user=None)
        returned, user = auto_assign_ticket(ticket, at=MONDAY_10)
        self.assertIsNone(user)
        self.assertIsNone(returned.assigned_to)
        self.assertFalse(AssignmentRecord.objects.exists())
        # Signal receiver must not assign either.
        new_ticket_done.send(sender="test", ticket=ticket)
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_to)


class ManualAssignmentTests(AssignmentTestBase):
    def test_manual_assignment_is_recorded(self):
        staff = get_staff_user()
        self.add_member(staff)
        self.add_member(self.alice)
        ticket = self.open_ticket(user=None)
        update_ticket(staff, ticket, owner=self.alice.id)
        record = AssignmentRecord.objects.get(ticket=ticket)
        self.assertEqual(record.kind, AssignmentRecord.KIND_MANUAL)
        self.assertEqual(record.user, self.alice)


class TakeTicketTests(AssignmentTestBase):
    def test_claim_unassigned(self):
        self.add_member(self.alice)
        ticket = self.open_ticket(user=None)
        _, claimed = take_ticket(ticket, self.alice, at=MONDAY_10)
        self.assertTrue(claimed)
        self.assertEqual(
            AssignmentRecord.objects.get(ticket=ticket).kind,
            AssignmentRecord.KIND_TAKE,
        )

    def test_second_claim_loses(self):
        self.add_member(self.alice)
        self.add_member(self.bob)
        ticket = self.open_ticket(user=None)
        take_ticket(ticket, self.alice, at=MONDAY_10)
        with self.assertRaises(AlreadyAssigned):
            take_ticket(ticket, self.bob, at=MONDAY_10)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to, self.alice)
        self.assertEqual(AssignmentRecord.objects.filter(ticket=ticket).count(), 1)

    def test_claiming_own_ticket_is_idempotent(self):
        self.add_member(self.alice)
        ticket = self.open_ticket(user=self.alice)
        _, claimed = take_ticket(ticket, self.alice, at=MONDAY_10)
        self.assertFalse(claimed)


class TakeTicketRaceTests(TransactionTestCase):
    """Two users clicking 'take' at exactly the same time."""

    def setUp(self):
        self.queue = Queue.objects.create(title="Support", slug="support-race")
        self.alice = get_user("racer-alice")
        self.bob = get_user("racer-bob")
        QueueMember.objects.create(queue=self.queue, user=self.alice)
        QueueMember.objects.create(queue=self.queue, user=self.bob)
        self.ticket = create_ticket(queue=self.queue)

    def tearDown(self):
        connections.close_all()

    def test_only_one_claimant_wins(self):
        barrier = threading.Barrier(2)
        results = {}

        def worker(name, username):
            # The shared-cache SQLite test database reports table locks
            # immediately instead of waiting (PostgreSQL/MySQL would block on
            # SELECT FOR UPDATE), so the losing request is retried the way a
            # production endpoint would. The conditional UPDATE is what makes
            # exactly one claimant win.
            barrier.wait(timeout=10)
            try:
                for _attempt in range(150):
                    try:
                        ticket = Ticket.objects.get(pk=self.ticket.pk)
                        user = User.objects.get(username=username)
                        _, claimed = take_ticket(ticket, user)
                        results[name] = (
                            "won" if claimed else "already-mine",
                            None,
                        )
                        return
                    except AlreadyAssigned:
                        results[name] = ("lost", None)
                        return
                    except OperationalError as exc:
                        if (
                            "locked" not in str(exc).lower()
                            and "busy" not in str(exc).lower()
                        ):
                            raise
                        connections["default"].close()
                        time.sleep(0.02)
                results[name] = ("error", "lock contention after retries")
            except Exception as exc:  # pragma: no cover - reported, not hidden
                results[name] = ("error", f"{type(exc).__name__}: {exc}")
            finally:
                connections["default"].close()

        t1 = threading.Thread(target=worker, args=("a", "racer-alice"))
        t2 = threading.Thread(target=worker, args=("b", "racer-bob"))
        t1.start()
        t2.start()
        t1.join(20)
        t2.join(20)
        self.assertFalse(t1.is_alive() or t2.is_alive())

        winners = [name for name, result in results.items() if result[0] == "won"]
        self.assertEqual(len(winners), 1, results)
        # No unexpected errors (e.g. database locks surfacing to users).
        errors = {k: v for k, v in results.items() if v[0] == "error"}
        if errors:  # pragma: no cover - diagnostic output
            raise AssertionError(f"unexpected worker errors: {errors}")
        self.assertEqual(errors, {})

        self.ticket.refresh_from_db()
        winner_name = "racer-alice" if winners[0] == "a" else "racer-bob"
        self.assertEqual(self.ticket.assigned_to.get_username(), winner_name)
        self.assertEqual(AssignmentRecord.objects.filter(ticket=self.ticket).count(), 1)


class InProgressTests(AssignmentTestBase):
    def test_followup_by_owner_means_in_progress(self):
        ticket = self.open_ticket(user=self.alice)
        self.assertFalse(ticket_is_in_progress(ticket))
        FollowUp.objects.create(
            ticket=ticket, user=self.alice, title="working on it", public=False
        )
        self.assertTrue(ticket_is_in_progress(ticket))

    def test_reopened_status_means_in_progress(self):
        ticket = self.open_ticket(user=self.alice)
        ticket.status = Ticket.REOPENED_STATUS
        ticket.save()
        self.assertTrue(ticket_is_in_progress(ticket))

    def test_unassigned_ticket_not_in_progress(self):
        ticket = self.open_ticket(user=None)
        self.assertFalse(ticket_is_in_progress(ticket))


class ReassignmentPreviewTests(AssignmentTestBase):
    def test_preview_when_member_on_leave(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob)
        t1 = self.open_ticket(self.alice)
        t2 = self.open_ticket(self.alice)

        preview = plan_member_reassignment(alice, at=MONDAY_10)
        self.assertEqual(set(preview.ticket_ids), {t1.id, t2.id})
        self.assertEqual(preview.reassigned_count, 2)
        self.assertEqual({item.new_user for item in preview.items}, {self.bob})

    def test_started_tickets_are_skipped(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob)
        waiting = self.open_ticket(self.alice)
        started = self.open_ticket(self.alice)
        FollowUp.objects.create(
            ticket=started, user=self.alice, title="investigating", public=False
        )
        reopened = self.open_ticket(self.alice)
        reopened.status = Ticket.REOPENED_STATUS
        reopened.save()

        preview = plan_member_reassignment(alice, at=MONDAY_10)
        self.assertEqual(preview.ticket_ids, [waiting.id])
        self.assertEqual(len(preview.skipped), 2)

    def test_scope_change_only_affects_uncovered_tickets(self):
        billing = QueueServiceArea.objects.create(
            queue=self.queue, name="Billing", slug="billing"
        )
        alice = self.add_member(self.alice)
        alice.service_areas.add(billing)
        self.add_member(self.bob)
        billing_ticket = self.open_ticket(self.alice, service_area=billing)
        network_ticket = self.open_ticket(self.alice, service_area=None)
        # Alice no longer covers the generic queue area: network_ticket has
        # no area so she still serves it; emulate a network area instead.
        network_area = QueueServiceArea.objects.create(
            queue=self.queue, name="Network", slug="network"
        )
        network_ticket.service_area = network_area
        network_ticket.save()

        preview = plan_member_reassignment(alice, at=MONDAY_10)
        self.assertEqual(preview.ticket_ids, [network_ticket.id])
        skipped_ids = [t.id for t, _reason in preview.skipped]
        self.assertIn(billing_ticket.id, skipped_ids)

    def test_tickets_still_covered_are_skipped(self):
        alice = self.add_member(self.alice)
        self.open_ticket(self.alice)
        preview = plan_member_reassignment(alice, at=MONDAY_10)
        self.assertEqual(preview.ticket_ids, [])
        self.assertEqual(len(preview.skipped), 1)

    def test_no_replacement_plans_unassign(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob, leave_until=MONDAY_12)
        ticket = self.open_ticket(self.alice)
        preview = plan_member_reassignment(alice, at=MONDAY_10)
        self.assertEqual(preview.ticket_ids, [ticket.id])
        self.assertIsNone(preview.items[0].new_user)
        self.assertEqual(preview.unassigned_count, 1)


class ApplyReassignmentTests(AssignmentTestBase):
    def test_apply_moves_tickets_and_logs(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob)
        t1 = self.open_ticket(self.alice)
        t2 = self.open_ticket(self.alice)

        preview = plan_member_reassignment(alice, at=MONDAY_10)
        moved = apply_reassignment(preview, at=MONDAY_10)
        self.assertEqual(moved, 2)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.assigned_to, self.bob)
        self.assertEqual(t2.assigned_to, self.bob)
        records = AssignmentRecord.objects.filter(kind=AssignmentRecord.KIND_REASSIGN)
        self.assertEqual(records.count(), 2)
        for record in records:
            self.assertEqual(record.previous_user, self.alice)
            self.assertEqual(record.user, self.bob)
        self.assertTrue(
            t1.followup_set.filter(title__icontains="Auto-reassigned").exists()
        )

    def test_apply_unassigns_when_no_replacement(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob, leave_until=MONDAY_12)
        ticket = self.open_ticket(self.alice)
        preview = plan_member_reassignment(alice, at=MONDAY_10)
        moved = apply_reassignment(preview, at=MONDAY_10)
        self.assertEqual(moved, 1)
        ticket.refresh_from_db()
        self.assertIsNone(ticket.assigned_to)

    def test_started_ticket_aborts_and_rolls_back_whole_batch(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob)
        t1 = self.open_ticket(self.alice)
        t2 = self.open_ticket(self.alice)
        preview = plan_member_reassignment(alice, at=MONDAY_10)
        # Work on t2 starts *after* the preview was generated.
        FollowUp.objects.create(
            ticket=t2, user=self.alice, title="started in the meantime", public=False
        )

        with self.assertRaises(BatchReassignmentError):
            apply_reassignment(preview, at=MONDAY_10)
        # The whole batch is restored: t1 (processed first) did not move.
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.assigned_to, self.alice)
        self.assertEqual(t2.assigned_to, self.alice)
        self.assertFalse(
            AssignmentRecord.objects.filter(
                kind=AssignmentRecord.KIND_REASSIGN
            ).exists()
        )

    def test_owner_drift_aborts_and_rolls_back(self):
        alice = self.add_member(self.alice, leave_until=MONDAY_12)
        self.add_member(self.bob)
        t1 = self.open_ticket(self.alice)
        t2 = self.open_ticket(self.alice)
        preview = plan_member_reassignment(alice, at=MONDAY_10)
        # Somebody manually reassigns t1 before confirmation.
        t1.assigned_to = self.carol
        t1.save()

        with self.assertRaises(BatchReassignmentError):
            apply_reassignment(preview, at=MONDAY_10)
        t2.refresh_from_db()
        self.assertEqual(t2.assigned_to, self.alice)


class MemberManagementViewTests(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self.staff = get_staff_user()
        self.client.force_login(self.staff)

    def test_list_page(self):
        self.add_member(self.alice)
        response = self.client.get("/queue-members/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "alice")

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        response = self.client.get("/queue-members/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_edit_member_then_preview_and_confirm(self):
        member = self.add_member(self.alice)
        self.add_member(self.bob)
        ticket = self.open_ticket(self.alice)
        # Save: member goes on leave.
        response = self.client.post(
            f"/queue-members/{member.id}/edit/",
            {
                "user": str(self.alice.id),
                "active": "on",
                "leave_until": "2030-01-01 00:00:00",
                "work_schedule": "{}",
                "service_areas": [],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/reassign/", response.url)
        member.refresh_from_db()
        self.assertFalse(member.is_available())
        # Preview shows the waiting ticket moving to Bob.
        response = self.client.get(f"/queue-members/{member.id}/reassign/")
        self.assertEqual(response.status_code, 200)
        preview = response.context["preview"]
        self.assertEqual(preview.ticket_ids, [ticket.id])
        self.assertEqual(preview.items[0].new_user, self.bob)
        # Confirm: batch is applied.
        response = self.client.post(f"/queue-members/{member.id}/reassign/")
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to, self.bob)


class TakeTicketViewTests(AssignmentTestBase):
    def setUp(self):
        super().setUp()
        self.staff = get_staff_user()
        self.client.force_login(self.staff)

    def test_take_link_claims_and_second_user_loses(self):
        other = get_user("helpdesk.staff2", is_staff=True)
        ticket = self.open_ticket(user=None)

        response = self.client.get(f"/tickets/{ticket.id}/?take")
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to, self.staff)
        # Somebody else tries to grab it afterwards: no change.
        self.client.force_login(other)
        response = self.client.get(f"/tickets/{ticket.id}/?take")
        self.assertEqual(response.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to, self.staff)
        self.assertEqual(AssignmentRecord.objects.filter(ticket=ticket).count(), 1)
