"""
Automatic ticket routing for queues.

This module is the single entry point for every operation that hands a
ticket to a user:

* :func:`auto_assign_ticket` - route a new, unassigned ticket;
* :func:`take_ticket` - a staff member claims an unassigned ticket;
* :func:`plan_member_reassignment` / :func:`apply_reassignment` - move the
  open, not-yet-started tickets of a member who went on leave or whose
  service scope changed.

Routing rules (in order):

1. Only active queue members who are currently available (not on leave and
   inside their working hours) and who serve the ticket's service area are
   candidates.
2. Candidates are ranked by their number of open tickets, then by the number
   of tickets assigned to them in the recent window, then by id for a stable
   tie-break.
3. Claiming a ticket runs in a database transaction that locks the row and
   performs a conditional UPDATE (``assigned_to IS NULL``), so two users
   racing for the same ticket can never both succeed.
4. A batch reassignment runs in one transaction: any failure rolls the whole
   batch back. Tickets whose owner has already started working on them are
   never moved, neither during preview nor when applying the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from django.db import transaction
from django.db.models import Count, Q
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext as _
from helpdesk import settings as helpdesk_settings
from helpdesk.lib import safe_template_context
from helpdesk.models import AssignmentRecord, FollowUp, Queue, QueueMember, Ticket
from helpdesk.signals import new_ticket_done


class AssignmentError(Exception):
    """Base class for assignment failures."""


class AlreadyAssigned(AssignmentError):
    """Raised when somebody else won the ticket between planning and saving."""


class BatchReassignmentError(AssignmentError):
    """Raised when a batch cannot be applied as planned; the batch is rolled
    back."""


@dataclass
class ReassignmentItem:
    ticket: Ticket
    new_user: object  # User or None (ticket returns to the unassigned pool)

    @property
    def will_unassign(self):
        return self.new_user is None


@dataclass
class ReassignmentPreview:
    member: QueueMember
    items: list[ReassignmentItem] = field(default_factory=list)
    skipped: list[tuple[Ticket, str]] = field(default_factory=list)

    @property
    def affected_tickets(self):
        return [item.ticket for item in self.items]

    @property
    def reassigned_count(self):
        return sum(1 for item in self.items if item.new_user is not None)

    @property
    def unassigned_count(self):
        return sum(1 for item in self.items if item.new_user is None)

    @property
    def ticket_ids(self):
        return [item.ticket.id for item in self.items]


def ticket_is_in_progress(ticket: Ticket) -> bool:
    """Whether work has visibly started on a ticket.

    A ticket counts as "being worked on" once its status has moved away from
    Open (e.g. Reopened) or the current owner has logged a follow-up on it.
    Such tickets must never be reassigned automatically.
    """
    if ticket.status != Ticket.OPEN_STATUS:
        return True
    if ticket.assigned_to_id is None:
        return False
    return FollowUp.objects.filter(
        ticket=ticket, user_id=ticket.assigned_to_id
    ).exists()


def _recent_window_start(at):
    return at - timedelta(
        minutes=helpdesk_settings.HELPDESK_AUTO_ASSIGN_RECENT_WINDOW_MINUTES
    )


def eligible_members(queue: Queue, ticket: Ticket | None = None, at=None):
    """Queue members who may receive ``ticket`` right now.

    Availability (active flag, leave, weekly working hours) and service-area
    coverage are evaluated in Python because they live in JSON / M2M data.
    """
    if at is None:
        at = timezone.now()
    members = (
        queue.members.filter(active=True)
        .select_related("user")
        .prefetch_related("service_areas")
    )
    result = []
    for member in members:
        if not member.is_available(at):
            continue
        if ticket is not None and not member.serves(ticket):
            continue
        result.append(member)
    return result


def choose_assignee(
    queue: Queue,
    ticket: Ticket | None = None,
    at=None,
    exclude_user_ids=(),
):
    """Pick the best available user for a ticket, or ``None``.

    Ranking key: (open ticket count, assignments in the recent window,
    membership id) - ascending on all three.
    """
    if at is None:
        at = timezone.now()
    candidates = eligible_members(queue, ticket, at)
    if exclude_user_ids:
        excluded = set(exclude_user_ids)
        candidates = [m for m in candidates if m.user_id not in excluded]
    if not candidates:
        return None

    since = _recent_window_start(at)
    loaded = {
        m.id: m
        for m in QueueMember.objects.filter(pk__in=[m.id for m in candidates]).annotate(
            open_count=Count(
                "user__assigned_to",
                filter=Q(user__assigned_to__status__in=Ticket.OPEN_STATUSES),
                distinct=True,
            ),
            recent_count=Count(
                "user__assignment_records",
                filter=Q(user__assignment_records__assigned_at__gte=since),
                distinct=True,
            ),
        )
    }
    candidates.sort(
        key=lambda m: (loaded[m.id].open_count, loaded[m.id].recent_count, m.id)
    )
    return candidates[0].user


def _owner_label(user):
    if user is None:
        return str(_("Unassigned"))
    return user.get_username()


def _log_owner_change(followup_title, ticket, new_user, previous_user=None, at=None):
    """Create a private audit follow-up (with Owner change record)."""
    followup = FollowUp(
        ticket=ticket,
        title=followup_title,
        date=at or timezone.now(),
        public=False,
    )
    # FollowUp.save() persists the ticket too.
    followup.save()
    followup.ticketchange_set.create(
        field=_("Owner"),
        old_value=_owner_label(previous_user),
        new_value=_owner_label(new_user),
    )
    return followup


def _notify_assigned_owner(ticket):
    """E-mail the new owner using the standard assignment template.

    Notifications are best-effort and must never break routing.
    """
    user = ticket.assigned_to
    if not user:
        return
    try:
        wants_mail = user.usersettings_helpdesk.email_on_ticket_assign
    except Exception:
        wants_mail = False
    if not wants_mail:
        return
    try:
        ticket.send(
            {"assigned_to": ("assigned_owner", safe_template_context(ticket))},
            fail_silently=True,
        )
    except Exception:
        pass


def _claim(ticket: Ticket, user, at=None, kind=AssignmentRecord.KIND_AUTO):
    """Conditionally assign an unassigned ticket.

    The WHERE clause on ``assigned_to IS NULL`` is the guarantee that only
    one of two concurrent transactions succeeds; ``select_for_update`` on the
    caller's transaction serialises them on databases that support it.
    """
    claimed = Ticket.objects.filter(pk=ticket.pk, assigned_to__isnull=True).update(
        assigned_to=user
    )
    if not claimed:
        raise AlreadyAssigned(
            _("Ticket %s was assigned by somebody else first.") % ticket.pk
        )
    ticket.assigned_to = user
    AssignmentRecord.objects.create(
        ticket=ticket,
        queue=ticket.queue,
        user=user,
        kind=kind,
        assigned_at=at or timezone.now(),
    )


def auto_assign_ticket(ticket: Ticket, at=None):
    """Route an unassigned ticket to the best available queue member.

    Returns ``(ticket, user)`` where ``user`` is ``None`` when nobody is
    eligible (the ticket simply stays unassigned). Raises
    :class:`AlreadyAssigned` if the ticket already has an owner.
    """
    if not helpdesk_settings.HELPDESK_AUTO_ASSIGN:
        return ticket, ticket.assigned_to
    if at is None:
        at = timezone.now()
    with transaction.atomic():
        locked = (
            Ticket.objects.select_for_update()
            .select_related("queue", "service_area")
            .get(pk=ticket.pk)
        )
        if locked.assigned_to_id is not None:
            raise AlreadyAssigned(_("Ticket %s already has an owner.") % locked.pk)
        user = choose_assignee(locked.queue, locked, at)
        if user is None:
            return locked, None
        _claim(locked, user, at=at, kind=AssignmentRecord.KIND_AUTO)
        _log_owner_change(
            _("Auto-assigned to %(username)s") % {"username": _owner_label(user)},
            locked,
            user,
            at=at,
        )
    _notify_assigned_owner(locked)
    return locked, user


def take_ticket(ticket: Ticket, user, at=None):
    """Let a staff member claim an unassigned ticket.

    Same conditional-update guarantee as auto-assignment: in a race, exactly
    one claimant wins.
    """
    if at is None:
        at = timezone.now()
    with transaction.atomic():
        locked = (
            Ticket.objects.select_for_update().select_related("queue").get(pk=ticket.pk)
        )
        if locked.assigned_to_id is not None:
            if locked.assigned_to_id == user.id:
                return locked, False
            raise AlreadyAssigned(
                _("Ticket %s was taken by somebody else first.") % locked.pk
            )
        _claim(locked, user, at=at, kind=AssignmentRecord.KIND_TAKE)
        _log_owner_change(
            _("Claimed by %(username)s") % {"username": _owner_label(user)},
            locked,
            user,
            at=at,
        )
    _notify_assigned_owner(locked)
    return locked, True


def plan_member_reassignment(
    member: QueueMember, service_area=None, at=None
) -> ReassignmentPreview:
    """Preview which waiting tickets of a member would move and to whom.

    Only open tickets still owned by the member are considered; tickets where
    work has started are listed under ``skipped`` and left untouched.
    """
    if at is None:
        at = timezone.now()
    if isinstance(service_area, int):
        service_area_id = service_area
    else:
        service_area_id = getattr(service_area, "id", None)

    tickets = (
        Ticket.objects.filter(
            queue=member.queue,
            assigned_to=member.user,
            status__in=Ticket.OPEN_STATUSES,
        )
        .select_related("service_area", "assigned_to")
        .order_by("id")
    )
    if service_area_id is not None:
        tickets = tickets.filter(service_area_id=service_area_id)

    preview = ReassignmentPreview(member=member)
    for ticket in tickets:
        if ticket_is_in_progress(ticket):
            preview.skipped.append(
                (ticket, _("Work on this ticket has already started."))
            )
            continue
        # Only tickets the member can no longer take (leave, outside working
        # hours, service scope removed) are affected.
        if member.is_available(at) and member.serves(ticket):
            preview.skipped.append(
                (ticket, _("The member can still handle this ticket."))
            )
            continue
        new_user = choose_assignee(
            member.queue,
            ticket,
            at,
            exclude_user_ids={member.user_id},
        )
        preview.items.append(ReassignmentItem(ticket=ticket, new_user=new_user))
    return preview


def apply_reassignment(preview: ReassignmentPreview, at=None):
    """Apply a previewed plan in a single transaction.

    Every ticket is re-locked and re-validated: ownership must not have
    changed since the preview and work must not have started. Any failure
    aborts and rolls back the *entire* batch.
    Returns the number of tickets that moved (including returns to the
    unassigned pool).
    """
    if at is None:
        at = timezone.now()
    moved = []
    with transaction.atomic():
        for item in preview.items:
            locked = Ticket.objects.select_for_update().get(pk=item.ticket.id)
            if locked.assigned_to_id != preview.member.user_id:
                raise BatchReassignmentError(
                    _(
                        "Ticket %s changed owner before the batch was applied; "
                        "the whole batch has been cancelled."
                    )
                    % locked.pk
                )
            if ticket_is_in_progress(locked):
                raise BatchReassignmentError(
                    _(
                        "Work has started on ticket %s; the whole batch has "
                        "been cancelled."
                    )
                    % locked.pk
                )

            if item.new_user is None:
                updated = Ticket.objects.filter(
                    pk=locked.pk, assigned_to_id=preview.member.user_id
                ).update(assigned_to=None)
                if not updated:
                    raise BatchReassignmentError(
                        _("Ticket %s could not be released.") % locked.pk
                    )
                locked.assigned_to = None
                _log_owner_change(
                    _(
                        "Returned to unassigned pool (member unavailable, no "
                        "eligible replacement)"
                    ),
                    locked,
                    None,
                    previous_user=preview.member.user,
                    at=at,
                )
            else:
                updated = Ticket.objects.filter(
                    pk=locked.pk, assigned_to_id=preview.member.user_id
                ).update(assigned_to=item.new_user)
                if not updated:
                    raise BatchReassignmentError(
                        _(
                            "Ticket %s changed owner during the batch; the "
                            "whole batch has been cancelled."
                        )
                        % locked.pk
                    )
                locked.assigned_to = item.new_user
                AssignmentRecord.objects.create(
                    ticket=locked,
                    queue=locked.queue,
                    user=item.new_user,
                    previous_user=preview.member.user,
                    kind=AssignmentRecord.KIND_REASSIGN,
                    assigned_at=at,
                )
                _log_owner_change(
                    _("Auto-reassigned to %(username)s")
                    % {"username": _owner_label(item.new_user)},
                    locked,
                    item.new_user,
                    previous_user=preview.member.user,
                    at=at,
                )
            moved.append(locked)

    for ticket in moved:
        _notify_assigned_owner(ticket)
    return len(moved)


@receiver(new_ticket_done)
def _auto_assign_on_new_ticket(sender, ticket=None, **kwargs):
    """Route tickets created through the web form or e-mail polling."""
    if not helpdesk_settings.HELPDESK_AUTO_ASSIGN:
        return
    if ticket is None or ticket.pk is None or ticket.assigned_to_id is not None:
        return
    auto_assign_ticket(ticket)
