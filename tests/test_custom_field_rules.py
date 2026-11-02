"""Conditional display/required rules for custom fields.

The same rules govern every entry point (public submission, staff entry,
ticket editing, the API and e-mail-created tickets). Old values are only
removed after an explicit confirmation, and such a clear is recorded in the
ticket history. Rule changes never rewrite history.
"""

import email
import logging
from typing import ClassVar

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from helpdesk import field_rules
from helpdesk.email import extract_email_metadata
from helpdesk.forms import (
    EditTicketCustomFieldForm,
    EditTicketForm,
    PublicTicketForm,
    TicketForm,
)
from helpdesk.models import (
    CustomField,
    CustomFieldRule,
    FollowUp,
    Queue,
    Ticket,
    TicketChange,
    TicketCustomFieldValue,
)

User = get_user_model()
logger = logging.getLogger("helpdesk")


class RuleEngineTestCase(TestCase):
    def setUp(self):
        self.q1 = Queue.objects.create(title="IT", slug="it")
        self.q2 = Queue.objects.create(title="HR", slug="hr")
        self.fault_type = CustomField.objects.create(
            name="fault_type",
            label="Fault Type",
            data_type="list",
            list_values="network\naccount",
        )
        self.department = CustomField.objects.create(
            name="department",
            label="Responsible Department",
            data_type="varchar",
            max_length=100,
        )
        self.always = CustomField.objects.create(
            name="cost_center",
            label="Cost Center",
            data_type="varchar",
            max_length=20,
            required=True,
        )

    def _network_required_rule(self, queue=None):
        return CustomFieldRule.objects.create(
            field=self.department,
            queue=queue,
            display_when=[],
            required_when=[
                {
                    "field": "custom_fault_type",
                    "operator": "eq",
                    "value": "network",
                }
            ],
        )

    def test_predicate_operators(self):
        context = field_rules.build_context(
            queue_id=3,
            priority=2,
            custom_values={"fault_type": "network", "order": "ORD-42"},
        )
        evaluate = field_rules.evaluate_predicates
        self.assertTrue(
            evaluate([{"field": "queue", "operator": "eq", "value": "3"}], context)
        )
        self.assertTrue(
            evaluate(
                [{"field": "queue", "operator": "in", "value": ["1", "3"]}],
                context,
            )
        )
        self.assertFalse(
            evaluate([{"field": "priority", "operator": "eq", "value": "4"}], context)
        )
        self.assertTrue(
            evaluate(
                [{"field": "custom_order", "operator": "contains", "value": "42"}],
                context,
            )
        )
        self.assertTrue(
            evaluate([{"field": "custom_fault_type", "operator": "filled"}], context)
        )
        self.assertTrue(
            evaluate(
                [
                    {
                        "field": "custom_missing",
                        "operator": "empty",
                    }
                ],
                context,
            )
        )
        self.assertTrue(evaluate([], context))

    def test_queue_specific_rule_overrides_global(self):
        CustomFieldRule.objects.create(
            field=self.department,
            queue=None,
            required_when=[
                {"field": "queue", "operator": "eq", "value": str(self.q2.id)}
            ],
        )
        self._network_required_rule(queue=self.q1)
        network_context = field_rules.build_context(
            self.q1.id, 3, {"fault_type": "network"}
        )
        states = field_rules.field_states(self.q1.id, network_context)
        self.assertTrue(states["department"]["required"])

        other_context = field_rules.build_context(
            self.q1.id, 3, {"fault_type": "account"}
        )
        states = field_rules.field_states(self.q1.id, other_context)
        self.assertFalse(states["department"]["required"])

    def test_static_required_falls_back_when_no_required_predicates(self):
        CustomFieldRule.objects.create(
            field=self.always,
            queue=self.q1,
            display_when=[{"field": "priority", "operator": "eq", "value": "1"}],
        )
        visible = field_rules.build_context(self.q1.id, 1, {})
        hidden = field_rules.build_context(self.q1.id, 3, {})
        self.assertTrue(
            field_rules.field_states(self.q1.id, visible)["cost_center"]["required"]
        )
        self.assertFalse(
            field_rules.field_states(self.q1.id, hidden)["cost_center"]["visible"]
        )

    def test_missing_required_fields_on_existing_ticket(self):
        self._network_required_rule()
        ticket = Ticket.objects.create(
            title="Net down", queue=self.q1, description="", priority=3
        )
        TicketCustomFieldValue.objects.create(
            ticket=ticket, field=self.fault_type, value="network"
        )
        # Statically required field is provided so only the conditional one
        # can be missing.
        TicketCustomFieldValue.objects.create(
            ticket=ticket, field=self.always, value="CC-1"
        )
        missing = field_rules.missing_required_fields(ticket)
        self.assertEqual([field.name for field in missing], ["department"])

        TicketCustomFieldValue.objects.create(
            ticket=ticket, field=self.department, value="Infra"
        )
        self.assertEqual(field_rules.missing_required_fields(ticket), [])

        # Condition not met -> not required any more.
        TicketCustomFieldValue.objects.filter(field=self.fault_type).update(
            value="account"
        )
        self.assertEqual(field_rules.missing_required_fields(ticket), [])

    def test_validate_predicates_rejects_bad_rules(self):
        with self.assertRaises(ValidationError):
            field_rules.validate_predicates(
                [{"field": "custom_unknown", "operator": "eq", "value": "x"}]
            )
        with self.assertRaises(ValidationError):
            field_rules.validate_predicates(
                [{"field": "queue", "operator": "near", "value": "1"}]
            )
        with self.assertRaises(ValidationError):
            field_rules.validate_predicates(
                [{"field": "queue", "operator": "in", "value": "1"}]
            )
        # Empty list is valid ("always").
        field_rules.validate_predicates([])

    def test_only_one_global_rule_per_field(self):
        CustomFieldRule.objects.create(field=self.department, queue=None)
        duplicate = CustomFieldRule(field=self.department, queue=None)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_preview_classifies_tickets_without_changing_them(self):
        # Rule: department shown/required only for network faults in IT.
        rule = CustomFieldRule(
            field=self.department,
            queue=self.q1,
            display_when=[
                {"field": "custom_fault_type", "operator": "eq", "value": "network"}
            ],
            required_when=[
                {"field": "custom_fault_type", "operator": "eq", "value": "network"}
            ],
        )
        hidden_with_value = Ticket.objects.create(
            title="Old ticket", queue=self.q1
        )
        TicketCustomFieldValue.objects.create(
            ticket=hidden_with_value, field=self.fault_type, value="account"
        )
        TicketCustomFieldValue.objects.create(
            ticket=hidden_with_value, field=self.department, value="Legacy Team"
        )
        required_missing = Ticket.objects.create(title="Net", queue=self.q1)
        TicketCustomFieldValue.objects.create(
            ticket=required_missing, field=self.fault_type, value="network"
        )
        other_queue = Ticket.objects.create(title="HR thing", queue=self.q2)

        result = field_rules.preview_rule(rule)
        preview_ids = {ticket.id for ticket in result["hidden_with_value"]}
        missing_ids = {ticket.id for ticket in result["required_missing"]}
        self.assertEqual(preview_ids, {hidden_with_value.id})
        self.assertEqual(missing_ids, {required_missing.id})
        self.assertEqual(result["total_scanned"], 2)
        # Preview is read-only, including across queue boundaries.
        self.assertNotIn(other_queue.id, preview_ids | missing_ids)
        self.assertTrue(
            TicketCustomFieldValue.objects.filter(
                ticket=hidden_with_value, field=self.department
            ).exists()
        )


class CreateFormRulesTestCase(TestCase):
    fixtures: ClassVar[list] = ["emailtemplate.json"]

    def setUp(self):
        self.q1 = Queue.objects.create(
            title="IT", slug="it", allow_public_submission=True
        )
        self.q2 = Queue.objects.create(title="HR", slug="hr")
        self.fault_type = CustomField.objects.create(
            name="fault_type",
            label="Fault Type",
            data_type="list",
            list_values="network\naccount",
        )
        self.department = CustomField.objects.create(
            name="department",
            label="Responsible Department",
            data_type="varchar",
            max_length=100,
        )
        CustomFieldRule.objects.create(
            field=self.department,
            display_when=[
                {"field": "custom_fault_type", "operator": "eq", "value": "network"}
            ],
            required_when=[
                {"field": "custom_fault_type", "operator": "eq", "value": "network"}
            ],
        )
        self.staff = User.objects.create(username="staff", is_staff=True)

    def base_data(self, **overrides):
        data = {
            "title": "Broken network",
            "queue": self.q1.id,
            "body": "Nothing works",
            "priority": 3,
            "submitter_email": "user@example.com",
        }
        data.update(overrides)
        return data

    def test_staff_form_requires_department_for_network_fault(self):
        form = TicketForm(
            data=self.base_data(custom_fault_type="network"),
            queue_choices=[(self.q1.id, self.q1.title)],
        )
        self.assertFalse(form.is_valid(), form.errors)
        self.assertIn("custom_department", form.errors)

    def test_staff_form_accepts_when_condition_not_met(self):
        form = TicketForm(
            data=self.base_data(custom_fault_type="account"),
            queue_choices=[(self.q1.id, self.q1.title)],
        )
        self.assertTrue(form.is_valid(), form.errors)
        ticket = form.save(user=self.staff)
        self.assertFalse(
            TicketCustomFieldValue.objects.filter(
                ticket=ticket, field=self.department
            ).exists()
        )

    def test_hidden_field_value_is_dropped_not_saved(self):
        # A tampered/manual POST carrying a value for a field the rules hide.
        form = TicketForm(
            data=self.base_data(
                custom_fault_type="account", custom_department="Sneaky"
            ),
            queue_choices=[(self.q1.id, self.q1.title)],
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn("department", form.invisible_custom_fields)
        ticket = form.save(user=self.staff)
        self.assertFalse(
            TicketCustomFieldValue.objects.filter(
                ticket=ticket, field=self.department
            ).exists()
        )

    def test_staff_form_saves_value_when_required_provided(self):
        form = TicketForm(
            data=self.base_data(
                custom_fault_type="network", custom_department="Infra"
            ),
            queue_choices=[(self.q1.id, self.q1.title)],
        )
        self.assertTrue(form.is_valid(), form.errors)
        ticket = form.save(user=self.staff)
        self.assertEqual(
            TicketCustomFieldValue.objects.get(
                ticket=ticket, field=self.department
            ).value,
            "Infra",
        )

    def test_public_form_uses_the_same_rules(self):
        form = PublicTicketForm(data=self.base_data(custom_fault_type="network"))
        self.assertFalse(form.is_valid())
        self.assertIn("custom_department", form.errors)

        form = PublicTicketForm(
            data=self.base_data(custom_fault_type="network", custom_department="NOC")
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_public_submission_end_to_end(self):
        def post(data):
            return self.client.post(reverse("helpdesk:home"), data, follow=True)

        response = post(self.base_data(custom_fault_type="network"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Responsible Department")
        self.assertIsNone(Ticket.objects.filter(title="Broken network").first())

        response = post(
            self.base_data(custom_fault_type="network", custom_department="NOC")
        )
        self.assertEqual(response.status_code, 200)
        ticket = Ticket.objects.get(title="Broken network")
        self.assertEqual(
            TicketCustomFieldValue.objects.get(
                ticket=ticket, field=self.department
            ).value,
            "NOC",
        )

    def test_staff_only_custom_fields_never_reach_public_rules(self):
        internal = CustomField.objects.create(
            name="internal_note",
            label="Internal",
            data_type="varchar",
            max_length=50,
            staff_only=True,
        )
        CustomFieldRule.objects.create(
            field=internal,
            required_when=[
                {"field": "queue", "operator": "eq", "value": str(self.q1.id)}
            ],
        )
        form = PublicTicketForm(data=self.base_data(custom_fault_type="account"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("custom_internal_note", form.fields)

    def test_widget_carries_rule_config(self):
        form = TicketForm(queue_choices=[(self.q1.id, self.q1.title)])
        widget = form.fields["custom_department"].widget
        self.assertIn("data-display-rules", widget.attrs)
        self.assertIn("data-required-rules", widget.attrs)
        self.assertEqual(
            widget.attrs["data-custom-field"], "department"
        )


class EditFormRulesTestCase(TestCase):
    fixtures: ClassVar[list] = ["emailtemplate.json"]

    def setUp(self):
        self.q1 = Queue.objects.create(title="IT", slug="it")
        self.staff = User.objects.create(username="staff", is_staff=True)
        self.fault_type = CustomField.objects.create(
            name="fault_type",
            label="Fault Type",
            data_type="list",
            list_values="network\naccount",
        )
        self.department = CustomField.objects.create(
            name="department",
            label="Responsible Department",
            data_type="varchar",
            max_length=100,
        )
        CustomFieldRule.objects.create(
            field=self.department,
            display_when=[
                {"field": "custom_fault_type", "operator": "eq", "value": "network"}
            ],
            required_when=[
                {"field": "custom_fault_type", "operator": "eq", "value": "network"}
            ],
        )
        self.ticket = Ticket.objects.create(
            title="Old issue", queue=self.q1, priority=3
        )
        TicketCustomFieldValue.objects.create(
            ticket=self.ticket, field=self.fault_type, value="account"
        )
        TicketCustomFieldValue.objects.create(
            ticket=self.ticket, field=self.department, value="Legacy Team"
        )

    def edit_data(self, **overrides):
        data = {
            "title": self.ticket.title,
            "queue": self.q1.id,
            "priority": 3,
            "submitter_email": "",
            "secret_key": self.ticket.secret_key,
            "custom_fault_type": "account",
            "custom_department": "Legacy Team",
        }
        data.update(overrides)
        return data

    def test_edit_form_offers_explicit_keep_or_clear_choice(self):
        form = EditTicketForm(instance=self.ticket)
        self.assertIn("custom_department_clear", form.fields)
        choice = form.fields["custom_department_clear"]
        self.assertEqual(
            choice.widget.attrs.get("data-clear-for"), "custom_department"
        )
        self.assertIn("department", form.pending_clear_choices)

    def test_keeping_old_value_leaves_it_untouched_without_history_rewrite(self):
        form = EditTicketForm(
            data=self.edit_data(), instance=self.ticket
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save(user=self.staff)
        value = TicketCustomFieldValue.objects.get(
            ticket=self.ticket, field=self.department
        )
        self.assertEqual(value.value, "Legacy Team")
        # No clearing follow-up / "removed" change was fabricated.
        self.assertFalse(
            TicketChange.objects.filter(
                followup__ticket=self.ticket,
                field="department",
                new_value="",
            ).exists()
        )

    def test_confirmed_clear_removes_value_but_keeps_it_in_history(self):
        form = EditTicketForm(
            data=self.edit_data(custom_department_clear="on"),
            instance=self.ticket,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save(user=self.staff)

        self.assertFalse(
            TicketCustomFieldValue.objects.filter(
                ticket=self.ticket, field=self.department
            ).exists()
        )
        change = TicketChange.objects.get(
            followup__ticket=self.ticket,
            field="department",
        )
        # History still proves the field was once filled.
        self.assertEqual(change.old_value, "Legacy Team")
        self.assertEqual(change.new_value, "")
        followup = change.followup
        self.assertFalse(followup.public)
        self.assertEqual(followup.ticketchange_set.count(), 1)

    def test_edit_view_confirmed_clear_end_to_end(self):
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("helpdesk:edit", args=(self.ticket.id,)),
            self.edit_data(custom_department_clear="on"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            TicketCustomFieldValue.objects.filter(
                ticket=self.ticket, field=self.department
            ).exists()
        )
        self.assertTrue(
            TicketChange.objects.filter(
                followup__ticket=self.ticket,
                field="department",
                old_value="Legacy Team",
                new_value="",
            ).exists()
        )

    def test_visible_required_field_blocks_edit(self):
        form = EditTicketForm(
            data=self.edit_data(
                custom_fault_type="network", custom_department=""
            ),
            instance=self.ticket,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("custom_department", form.errors)

    def test_respond_form_keeps_hidden_old_value_without_confirmation(self):
        # The respond form is built from the main form's cleaned_data dict.
        main_form = TicketForm(
            data={
                "title": self.ticket.title,
                "queue": self.q1.id,
                "priority": 3,
                "body": "",
                "submitter_email": "",
                "custom_fault_type": "account",
                "custom_department": "Legacy Team",
            },
            queue_choices=[(self.q1.id, self.q1.title)],
            body_reqd=False,
        )
        self.assertTrue(main_form.is_valid(), main_form.errors)
        respond_form = EditTicketCustomFieldForm(
            main_form.cleaned_data, instance=self.ticket
        )
        self.assertTrue(respond_form.is_valid(), respond_form.errors)
        self.assertNotIn("custom_department_clear", respond_form.fields)
        followup = FollowUp.objects.create(
            ticket=self.ticket, title="Updated", public=True
        )
        respond_form.save(followup=followup)
        # Value survives; no fabricated change for an untouched hidden field.
        self.assertEqual(
            TicketCustomFieldValue.objects.get(
                ticket=self.ticket, field=self.department
            ).value,
            "Legacy Team",
        )
        self.assertFalse(
            TicketChange.objects.filter(
                followup=followup, field="department"
            ).exists()
        )


class EmailEntryRulesTestCase(TestCase):
    fixtures: ClassVar[list] = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Mail IT",
            slug="mit",
            allow_email_submission=True,
            email_address="it@example.com",
        )
        self.department = CustomField.objects.create(
            name="department",
            label="Responsible Department",
            data_type="varchar",
            max_length=100,
        )
        CustomFieldRule.objects.create(
            field=self.department,
            queue=self.queue,
            # Unconditionally required for tickets polled from this mailbox.
            required_when=[
                {"field": "queue", "operator": "eq", "value": str(self.queue.id)},
            ],
        )

    def _email_message(self):
        msg = email.message.Message()
        msg["Subject"] = "Help needed"
        msg["From"] = "sender@example.com"
        msg["To"] = self.queue.email_address
        msg["Content-Type"] = "text/plain;"
        msg.set_payload("Something broke.")
        return msg

    def test_email_ticket_gets_private_note_about_missing_fields(self):
        extract_email_metadata(str(self._email_message()), self.queue, logger=logger)
        ticket = Ticket.objects.get(queue=self.queue, title="Help needed")
        notes = ticket.followup_set.filter(public=False)
        note = notes.get(title="Missing required information")
        self.assertIn("Responsible Department", note.comment)

    def test_no_note_when_nothing_required(self):
        CustomFieldRule.objects.all().delete()
        extract_email_metadata(str(self._email_message()), self.queue, logger=logger)
        ticket = Ticket.objects.get(queue=self.queue, title="Help needed")
        self.assertFalse(
            ticket.followup_set.filter(
                title="Missing required information"
            ).exists()
        )


class AdminPreviewTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="root", email="root@example.com", password="pw"
        )
        self.q1 = Queue.objects.create(title="IT", slug="it")
        self.department = CustomField.objects.create(
            name="department",
            label="Department",
            data_type="varchar",
            max_length=100,
        )
        self.rule = CustomFieldRule.objects.create(
            field=self.department,
            queue=self.q1,
            display_when=[
                {"field": "priority", "operator": "eq", "value": "1"}
            ],
        )
        self.ticket = Ticket.objects.create(
            title="Urgent legacy", queue=self.q1, priority=3
        )
        TicketCustomFieldValue.objects.create(
            ticket=self.ticket, field=self.department, value="Legacy"
        )

    def test_preview_saved_rule(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse(
                "admin:helpdesk_customfieldrule_preview", args=(self.rule.id,)
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Urgent legacy")

    def test_preview_unsaved_changes_does_not_save(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse(
                "admin:helpdesk_customfieldrule_change", args=(self.rule.id,)
            ),
            {
                "field": self.department.id,
                "queue": self.q1.id,
                # Unsaved condition still hides the priority-3 ticket.
                "display_when": '[{"field": "priority", "operator": "eq", "value": "2"}]',
                "required_when": "[]",
                "enabled": "on",
                "_preview": "1",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Urgent legacy")
        self.rule.refresh_from_db()
        # The previewed condition must not have been persisted.
        self.assertEqual(
            self.rule.display_when,
            [{"field": "priority", "operator": "eq", "value": "1"}],
        )

    def test_custom_field_admin_embeds_rules_inline(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("admin:helpdesk_customfield_change", args=(self.department.id,))
        )
        self.assertEqual(response.status_code, 200)
