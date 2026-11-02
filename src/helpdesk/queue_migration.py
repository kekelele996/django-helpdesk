"""
queue_migration.py - Batch ticket migration between queues.

Flow:
  1. create_preview() snapshots the selected tickets and their queues into a
     QueueMigration / QueueMigrationItem pair.
  2. build_preview_data() turns that snapshot into the preview shown to the
     operator: field mapping, target queue access holders and followers who
     lose their staff entry point.
  3. perform_migration() moves every ticket inside one transaction. Any error
     rolls the whole batch back to its original state; a completed migration
     is a no-op when confirmed again, and tickets already sitting in the
     target queue produce no change records.

Nothing here grants or revokes queue permissions, removes TicketCC rows,
sends e-mail or touches historical follow-ups.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from helpdesk import settings as helpdesk_settings
from helpdesk.decorators import is_helpdesk_staff
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
from helpdesk.user import HelpdeskUser


User = get_user_model()


class QueueMigrationError(Exception):
    """Expected failure: the batch stays untouched and the message is shown."""


class MigrationConflict(QueueMigrationError):
    """A ticket changed between the preview and confirmation."""


class MigrationPermissionError(QueueMigrationError):
    """The operator may not move this batch."""


def _ensure_staff(operator) -> None:
    if not (operator and operator.is_authenticated and is_helpdesk_staff(operator)):
        raise MigrationPermissionError(_("Only helpdesk staff may migrate queues."))


def _assert_queue_access(huser: HelpdeskUser, queues) -> None:
    for queue in dict.fromkeys(queues):
        if not huser.can_access_queue(queue):
            raise MigrationPermissionError(
                _('You do not have access to queue "%(queue)s".')
                % {"queue": queue.title}
            )


def create_preview(*, operator, tickets, target_queue: Queue, reason: str = ""):
    """Validate the selection and snapshot it into a QueueMigration."""
    _ensure_staff(operator)

    ticket_ids = [ticket.pk for ticket in tickets]
    if not ticket_ids:
        raise QueueMigrationError(_("Please select at least one ticket."))
    # Re-fetch from the database: callers may hand over stale instances whose
    # queue foreign key cache no longer matches the stored value.
    fresh_tickets = list(
        Ticket.objects.select_related("queue").filter(pk__in=ticket_ids)
    )
    if len(fresh_tickets) != len(set(ticket_ids)):
        raise QueueMigrationError(
            _("At least one selected ticket no longer exists.")
        )
    fresh_tickets.sort(key=lambda t: ticket_ids.index(t.pk))

    huser = HelpdeskUser(operator)
    if not huser.can_access_queue(target_queue):
        raise MigrationPermissionError(
            _('You do not have access to the target queue "%(queue)s".')
            % {"queue": target_queue.title}
        )
    _assert_queue_access(huser, [t.queue for t in fresh_tickets])

    with transaction.atomic():
        migration = QueueMigration.objects.create(
            target_queue=target_queue,
            created_by=operator,
            reason=reason.strip()[:200],
        )
        QueueMigrationItem.objects.bulk_create(
            [
                QueueMigrationItem(
                    migration=migration,
                    ticket=ticket,
                    source_queue=ticket.queue,
                )
                for ticket in fresh_tickets
            ]
        )
    return migration


def _queue_permission_holders(queue: Queue):
    """Active users who effectively hold a queue's access permission.

    Superusers are included because has_perm() passes for them; group-based
    grants are read through groups__permissions, which is how non-trivial
    installations hand out queue access.
    """
    codename = queue.permission_name.removeprefix("helpdesk.")
    return (
        User.objects.filter(
            Q(is_superuser=True)
            | Q(user_permissions__codename=codename)
            | Q(groups__permissions__codename=codename),
            is_active=True,
        )
        .distinct()
        .order_by("username")
    )


def _field_mapping_rows(source_queues, target_queue):
    """Static explanation of how every piece of ticket data is handled."""
    source_titles = ", ".join(sorted(q.title for q in source_queues))
    return [
        {
            "field": _("Standard fields (title, description, status, priority, "
                       "submitter, owner, due date, resolution, created date)"),
            "treatment": _("Kept unchanged"),
        },
        {
            "field": _("Queue"),
            "treatment": _("%(source)s → %(target)s (the only field changed)")
            % {"source": source_titles, "target": target_queue.title},
        },
        {
            "field": _("Custom fields"),
            "treatment": _(
                "Custom fields are global and shared by every queue; every "
                "field value is kept on its ticket unchanged."
            ),
        },
        {
            "field": _("Follow-ups / historical replies"),
            "treatment": _("Kept unchanged; one private migration note is added"),
        },
        {
            "field": _("Attachments"),
            "treatment": _(
                "Kept unchanged and stay downloadable from their stored path"
            ),
        },
        {
            "field": _("CC / followers"),
            "treatment": _("Kept unchanged (access changes are listed below)"),
        },
        {
            "field": _("Dependencies and checklists"),
            "treatment": _("Kept unchanged"),
        },
        {
            "field": _("Queue notification addresses (new/updated ticket CC)"),
            "treatment": _(
                "Belong to the queue; future notifications follow the target "
                "queue's configuration (listed below)"
            ),
        },
    ]


def build_preview_data(migration: QueueMigration) -> dict:
    """Assemble everything the preview / result page displays."""
    target_queue = migration.target_queue
    items_qs = migration.items.select_related(
        "ticket", "source_queue", "ticket__queue", "ticket__assigned_to"
    ).order_by("id")

    ticket_ids = [item.ticket_id for item in items_qs]
    tickets = {item.ticket_id: item.ticket for item in items_qs}
    # Batch the per-ticket counters instead of querying per row.
    attachment_counts = Counter(
        FollowUpAttachment.objects.filter(followup__ticket_id__in=ticket_ids)
        .values_list("followup__ticket_id", flat=True)
    )
    followup_counts = Counter(
        FollowUp.objects.filter(ticket_id__in=ticket_ids)
        .values_list("ticket_id", flat=True)
    )
    custom_value_counts = Counter(
        TicketCustomFieldValue.objects.filter(ticket_id__in=ticket_ids)
        .values_list("ticket_id", flat=True)
    )
    dependency_counts = Counter(
        TicketDependency.objects.filter(ticket_id__in=ticket_ids)
        .values_list("ticket_id", flat=True)
    )
    checklist_counts = Counter(
        Checklist.objects.filter(ticket_id__in=ticket_ids)
        .values_list("ticket_id", flat=True)
    )

    cc_users_by_ticket = defaultdict(list)
    cc_emails_by_ticket = defaultdict(list)
    for cc in TicketCC.objects.filter(ticket_id__in=ticket_ids).select_related(
        "user"
    ):
        if cc.user_id:
            cc_users_by_ticket[cc.ticket_id].append(cc.user)
        else:
            cc_emails_by_ticket[cc.ticket_id].append(cc.email_address)

    per_queue_mode = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
    source_queues = list(
        Queue.objects.filter(
            pk__in={item.source_queue_id for item in items_qs}
        ).order_by("title")
    )
    # Target-queue access: holders, and which of them already had access to
    # every source queue (i.e. do not newly receive the tickets).
    recipients_new, recipients_existing = [], []
    losing_followers_ids = set()
    losing_owners = {}
    if per_queue_mode:
        for holder in _queue_permission_holders(target_queue):
            had_all_sources = all(
                HelpdeskUser(holder).can_access_queue(q) for q in source_queues
            )
            (recipients_existing if had_all_sources else recipients_new).append(
                holder
            )

    item_rows = []
    for item in items_qs:
        ticket = tickets[item.ticket_id]
        losing_cc_users = []
        if per_queue_mode:
            for cc_user in cc_users_by_ticket[item.ticket_id]:
                if not HelpdeskUser(cc_user).can_access_queue(target_queue):
                    losing_cc_users.append(cc_user)
                    losing_followers_ids.add(cc_user.id)
        owner = ticket.assigned_to
        owner_loses = bool(
            per_queue_mode
            and owner
            and not HelpdeskUser(owner).can_access_queue(target_queue)
        )
        if owner_loses:
            losing_owners[owner.id] = owner

        item_rows.append(
            {
                "item": item,
                "ticket": ticket,
                "source_queue": item.source_queue,
                "already_in_target": item.source_queue_id == target_queue.id,
                "cc_users": cc_users_by_ticket[item.ticket_id],
                "cc_emails": cc_emails_by_ticket[item.ticket_id],
                "losing_cc_users": losing_cc_users,
                "owner": owner,
                "owner_loses_access": owner_loses,
                "counts": {
                    "attachments": attachment_counts.get(item.ticket_id, 0),
                    "followups": followup_counts.get(item.ticket_id, 0),
                    "cc_users": len(cc_users_by_ticket[item.ticket_id]),
                    "cc_emails": len(cc_emails_by_ticket[item.ticket_id]),
                    "custom_values": custom_value_counts.get(item.ticket_id, 0),
                    "dependencies": dependency_counts.get(item.ticket_id, 0),
                    "checklists": checklist_counts.get(item.ticket_id, 0),
                },
            }
        )

    losing_followers = list(
        User.objects.filter(id__in=losing_followers_ids).order_by("username")
    )

    moving_count = sum(1 for row in item_rows if not row["already_in_target"])
    totals = {
        "tickets": len(item_rows),
        "moving": moving_count,
        "already_in_target": len(item_rows) - moving_count,
        "attachments": sum(row["counts"]["attachments"] for row in item_rows),
        "followups": sum(row["counts"]["followups"] for row in item_rows),
        "cc_emails": sum(row["counts"]["cc_emails"] for row in item_rows),
    }
    # Source queues first, target last; de-duplicated while preserving order.
    queue_cc_info = []
    seen_queue_ids = set()
    for q in source_queues:
        if q.id in seen_queue_ids:
            continue
        seen_queue_ids.add(q.id)
        queue_cc_info.append(
            {
                "queue": q,
                "new_ticket_cc": q.new_ticket_cc or "",
                "updated_ticket_cc": q.updated_ticket_cc or "",
            }
        )
    if target_queue.id not in seen_queue_ids:
        queue_cc_info.append(
            {
                "queue": target_queue,
                "new_ticket_cc": target_queue.new_ticket_cc or "",
                "updated_ticket_cc": target_queue.updated_ticket_cc or "",
            }
        )

    return {
        "migration": migration,
        "target_queue": target_queue,
        "source_queues": source_queues,
        "per_queue_mode": per_queue_mode,
        "item_rows": item_rows,
        "field_mapping_rows": _field_mapping_rows(source_queues, target_queue),
        "custom_fields": list(CustomField.objects.all()),
        "recipients_new": recipients_new,
        "recipients_existing": recipients_existing,
        "losing_followers": losing_followers,
        "losing_owners": list(losing_owners.values()),
        "queue_cc_info": queue_cc_info,
        "totals": totals,
    }


def perform_migration(operator, migration: QueueMigration) -> dict:
    """Execute a previewed migration atomically.

    Returns a summary {"migrated": [items], "unchanged": [items]}.
    Re-running a completed migration is an idempotent no-op.
    """
    _ensure_staff(operator)
    huser = HelpdeskUser(operator)

    with transaction.atomic():
        try:
            locked_migration = (
                QueueMigration.objects.select_for_update()
                .select_related("target_queue")
                .get(pk=migration.pk)
            )
        except QueueMigration.DoesNotExist:
            raise QueueMigrationError(_("This migration no longer exists."))

        if locked_migration.status == QueueMigration.COMPLETED:
            items = list(locked_migration.items.order_by("id"))
            return {
                "migrated": [i for i in items if i.status == QueueMigrationItem.MIGRATED],
                "unchanged": [
                    i for i in items if i.status != QueueMigrationItem.MIGRATED
                ],
            }
        if locked_migration.status == QueueMigration.CANCELLED:
            raise QueueMigrationError(
                _("This migration preview was cancelled and cannot be executed.")
            )

        target_queue = locked_migration.target_queue
        if not huser.can_access_queue(target_queue):
            raise MigrationPermissionError(
                _('You do not have access to the target queue "%(queue)s".')
                % {"queue": target_queue.title}
            )

        items = list(
            locked_migration.items.select_related("ticket", "source_queue").order_by(
                "id"
            )
        )
        source_queues = {item.source_queue for item in items if item.source_queue}
        _assert_queue_access(huser, source_queues)

        ticket_ids = [item.ticket_id for item in items]
        locked_tickets = {
            t.id: t
            for t in Ticket.objects.select_for_update()
            .select_related("queue")
            .filter(id__in=ticket_ids)
        }
        if set(locked_tickets) != set(ticket_ids):
            raise MigrationConflict(
                _("At least one ticket of this batch no longer exists.")
            )
        # Nothing may have happened to the tickets since the preview.
        for item in items:
            ticket = locked_tickets[item.ticket_id]
            if (
                item.source_queue_id is not None
                and ticket.queue_id != item.source_queue_id
            ):
                raise MigrationConflict(
                    _(
                        'Ticket #%(ticket_id)s was moved to queue "%(queue)s" '
                        "after this preview was created. Re-open the preview and "
                        "confirm the current selection again."
                    )
                    % {"ticket_id": ticket.id, "queue": ticket.queue.title}
                )

        migrated, unchanged = [], []
        try:
            for item in items:
                ticket = locked_tickets[item.ticket_id]
                if ticket.queue_id == target_queue.id:
                    # Idempotency guard for already-relocated tickets: no
                    # follow-up or ticket change is written.
                    item.status = QueueMigrationItem.UNCHANGED
                    item.save(update_fields=["status"])
                    unchanged.append(item)
                    continue

                old_queue_id = ticket.queue_id
                ticket.queue_id = target_queue.id
                title = _(
                    'Moved to queue "%(queue)s" by batch migration #%(migration_id)s'
                ) % {
                    "queue": target_queue.title,
                    "migration_id": locked_migration.id,
                }
                followup = FollowUp(
                    ticket=ticket,
                    date=timezone.now(),
                    user=operator,
                    public=False,
                    title=title,
                    comment=locked_migration.reason or None,
                    email_recipients=[],
                )
                followup.save()  # FollowUp.save() persists the queue change
                TicketChange.objects.create(
                    followup=followup,
                    field=_("Queue"),
                    old_value=str(old_queue_id),
                    new_value=str(target_queue.id),
                )

                item.status = QueueMigrationItem.MIGRATED
                item.followup = followup
                item.save(update_fields=["status", "followup"])
                migrated.append(item)

            locked_migration.status = QueueMigration.COMPLETED
            locked_migration.confirmed_at = timezone.now()
            locked_migration.save(update_fields=["status", "confirmed_at"])
        except (MigrationConflict, MigrationPermissionError):
            raise
        except Exception as exc:
            # Exiting the atomic block with an exception rolls every ticket,
            # follow-up and item update back; surface a showable message.
            raise QueueMigrationError(
                _("Unexpected error while migrating the tickets: %(error)s")
                % {"error": exc}
            ) from exc

        return {"migrated": migrated, "unchanged": unchanged}


def cancel_preview(operator, migration: QueueMigration) -> None:
    """Mark a previewed migration as cancelled without touching any ticket."""
    _ensure_staff(operator)
    with transaction.atomic():
        try:
            locked = QueueMigration.objects.select_for_update().get(pk=migration.pk)
        except QueueMigration.DoesNotExist:
            raise QueueMigrationError(_("This migration no longer exists."))
        if locked.status != QueueMigration.PREVIEWED:
            raise QueueMigrationError(
                _("Only pending previews can be cancelled.")
            )
        locked.status = QueueMigration.CANCELLED
        locked.save(update_fields=["status"])
