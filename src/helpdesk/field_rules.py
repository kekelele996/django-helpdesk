"""
Conditional display/required rules for custom fields.

This module is the single source of truth shared by every entry point that
creates or updates tickets:

* the public submission form (:class:`helpdesk.forms.PublicTicketForm`),
* staff ticket creation and the edit page,
* the ticket respond form on the ticket page,
* the REST API (which validates through ``TicketForm``), and
* tickets created from incoming e-mail.

Rules live on :class:`helpdesk.models.CustomFieldRule`. A predicate is a
JSON dict ``{"field": ..., "operator": ..., "value": ...}`` where ``field``
is either ``queue``, ``priority`` or ``custom_<name>``. All predicates in a
list must match (AND). Empty predicate lists mean "always".
"""

import logging

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from helpdesk.lib import convert_value
from helpdesk.models import (
    CustomField,
    CustomFieldRule,
    FollowUp,
    TicketCustomFieldValue,
)

logger = logging.getLogger(__name__)

#: Queue key used in widget configs for the global (queue-less) rule.
WILDCARD_QUEUE = "*"

OP_EQUALS = "eq"
OP_NOT_EQUALS = "ne"
OP_IN = "in"
OP_NOT_IN = "not_in"
OP_CONTAINS = "contains"
OP_EMPTY = "empty"
OP_FILLED = "filled"

OPERATORS = frozenset(
    {
        OP_EQUALS,
        OP_NOT_EQUALS,
        OP_IN,
        OP_NOT_IN,
        OP_CONTAINS,
        OP_EMPTY,
        OP_FILLED,
    }
)
VALUELESS_OPERATORS = frozenset({OP_EMPTY, OP_FILLED})

#: Ticket fields that may be used as predicate sources, besides custom_*.
BUILTIN_PREDICATE_FIELDS = frozenset({"queue", "priority"})


def normalize_value(value):
    """Normalize any form/storage value to the string used by predicates.

    Custom field values are stored as text; booleans are stored as the
    strings ``"True"`` / ``"False"`` (see ``convert_value``), so comparisons
    must use the same representation on both sides.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return value
    converted = convert_value(value)
    return str(converted if converted is not None else "")


def validate_predicates(predicates):
    """Validate a predicate list, as stored on a rule.

    Used by ``CustomFieldRule.clean()`` and the admin form. Raises
    ``ValidationError`` with a list of messages on the first structural
    problem found.
    """
    if predicates in (None, []):
        return
    if not isinstance(predicates, list):
        raise ValidationError(
            _("Predicates must be a JSON list of objects."),
            code="invalid-predicates",
        )

    valid_names = set(CustomField.objects.values_list("name", flat=True))

    for index, predicate in enumerate(predicates):
        where = _("Predicate %(index)d") % {"index": index + 1}
        if not isinstance(predicate, dict):
            raise ValidationError(
                _("%(where)s must be an object.") % {"where": where},
                code="invalid-predicate",
            )
        source = predicate.get("field")
        operator = predicate.get("operator", OP_EQUALS)
        if not source or not isinstance(source, str):
            raise ValidationError(
                _("%(where)s is missing its 'field'.") % {"where": where},
                code="invalid-predicate-field",
            )
        if operator not in OPERATORS:
            raise ValidationError(
                _("%(where)s uses an unknown operator '%(operator)s'.")
                % {"where": where, "operator": operator},
                code="invalid-predicate-operator",
            )
        if source not in BUILTIN_PREDICATE_FIELDS:
            if not source.startswith("custom_"):
                raise ValidationError(
                    _(
                        "%(where)s: '%(field)s' is not a valid predicate "
                        "field; use 'queue', 'priority' or 'custom_<name>'."
                    )
                    % {"where": where, "field": source},
                    code="invalid-predicate-field",
                )
            if source[7:] not in valid_names:
                raise ValidationError(
                    _(
                        "%(where)s references unknown custom field '%(field)s'."
                    )
                    % {"where": where, "field": source},
                    code="invalid-predicate-field",
                )
        if operator in VALUELESS_OPERATORS:
            continue
        value = predicate.get("value")
        if operator in (OP_IN, OP_NOT_IN):
            if not isinstance(value, list) or not value:
                raise ValidationError(
                    _("%(where)s: operator '%(operator)s' needs a non-empty "
                      "list of values.")
                    % {"where": where, "operator": operator},
                    code="invalid-predicate-value",
                )
        elif value is None or value == "":
            raise ValidationError(
                _("%(where)s is missing its 'value'.") % {"where": where},
                code="invalid-predicate-value",
            )


def build_context(queue_id=None, priority=None, custom_values=None):
    """Build the predicate context for a (partially filled) ticket.

    ``custom_values`` may use either ``custom_<name>`` or bare ``<name>``
    keys, with raw form or stored values.
    """
    context = {
        "queue": "" if queue_id in (None, "") else str(queue_id),
        "priority": "" if priority in (None, "") else str(priority),
    }
    for name, value in (custom_values or {}).items():
        key = name if name.startswith("custom_") else f"custom_{name}"
        context[key] = normalize_value(value)
    return context


def _predicate_matches(predicate, context):
    source = predicate.get("field", "")
    operator = predicate.get("operator", OP_EQUALS)
    actual = context.get(source, "")
    if operator == OP_EMPTY:
        return actual == ""
    if operator == OP_FILLED:
        return actual != ""
    expected = predicate.get("value", "")
    if operator == OP_CONTAINS:
        return normalize_value(expected) in actual
    if operator == OP_IN:
        return actual in [normalize_value(item) for item in expected]
    if operator == OP_NOT_IN:
        return actual not in [normalize_value(item) for item in expected]
    if operator == OP_NOT_EQUALS:
        return actual != normalize_value(expected)
    return actual == normalize_value(expected)


def evaluate_predicates(predicates, context):
    """All predicates match (AND). An empty list always matches."""
    if not predicates:
        return True
    return all(_predicate_matches(predicate, context) for predicate in predicates)


def rules_for_queue(queue_id):
    """Map custom field name -> effective rule in the given queue.

    A queue-specific rule always wins over the global (queue-less) rule.
    """
    slots = {}
    for rule in (
        CustomFieldRule.objects.filter(enabled=True)
        .select_related("field")
        .order_by("id")
    ):
        slot = slots.setdefault(rule.field.name, {})
        if rule.queue_id is None:
            slot.setdefault("global", rule)
        elif rule.queue_id == queue_id:
            slot["queue"] = rule
    return {
        name: chosen.get("queue") or chosen.get("global")
        for name, chosen in slots.items()
        if chosen.get("queue") or chosen.get("global")
    }


def field_states(queue_id, context, field_queryset=None):
    """Return ``{field_name: {"visible": bool, "required": bool}}``.

    Combines rules with the static ``CustomField.required`` flag so callers
    never have to merge anything themselves.
    """
    if field_queryset is None:
        field_queryset = CustomField.objects.all()
    rules = rules_for_queue(queue_id)
    states = {}
    for custom_field in field_queryset:
        rule = rules.get(custom_field.name)
        visible = True
        required = custom_field.required
        if rule is not None:
            visible = evaluate_predicates(rule.display_when, context)
            if rule.required_when:
                required = visible and evaluate_predicates(
                    rule.required_when, context
                )
        states[custom_field.name] = {"visible": visible, "required": required}
    return states


def ticket_custom_values(ticket):
    """Raw stored custom field values of a ticket as ``{name: value}``."""
    return dict(
        TicketCustomFieldValue.objects.filter(ticket=ticket).values_list(
            "field__name", "value"
        )
    )


def ticket_context(ticket, extra_values=None):
    values = ticket_custom_values(ticket)
    if extra_values:
        values.update(extra_values)
    return build_context(ticket.queue_id, ticket.priority, values)


def _stored_value_is_empty(custom_field, value):
    if value is None or value == "":
        return True
    if custom_field.data_type == "boolean":
        return value != "True"
    return False


def missing_required_fields(ticket):
    """Custom fields the ticket must provide under current rules but doesn't.

    Works off the ticket's stored values, so it also covers tickets that
    arrived through e-mail (which cannot supply custom fields at all).
    """
    values = ticket_custom_values(ticket)
    context = build_context(ticket.queue_id, ticket.priority, values)
    fields = list(CustomField.objects.all())
    states = field_states(
        ticket.queue_id,
        context,
        field_queryset=fields,
    )
    by_name = {field.name: field for field in fields}
    missing = []
    for name, state in states.items():
        if state["visible"] and state["required"]:
            if _stored_value_is_empty(by_name[name], values.get(name)):
                missing.append(by_name[name])
    return missing


def note_missing_required_fields(ticket, logger=None):
    """Leave a private follow-up listing required fields still missing.

    Used for e-mail-created tickets, where the sender cannot be bounced back
    interactively: triaging staff see the gap on the ticket instead of
    discovering it at handoff. Returns the follow-up (or ``None``).
    """
    missing = missing_required_fields(ticket)
    if not missing:
        return None
    labels = ", ".join(str(field.label) for field in missing)
    followup = FollowUp.objects.create(
        ticket=ticket,
        date=timezone.now(),
        title=_("Missing required information"),
        comment=_(
            "This ticket was created without the following required "
            "information: %(fields)s"
        )
        % {"fields": labels},
        public=False,
    )
    if logger is not None:
        logger.info(
            "Ticket %s is missing required custom fields: %s",
            ticket.id,
            labels,
        )
    return followup


def widget_rules_config(field_names):
    """Per-field predicate config for the client-side form JavaScript.

    Shape::

        {"field_name": {"display": {"*": [...], "7": [...]},
                        "required": {"*": [...], "7": [...]}}}

    ``*`` holds the global (queue-less) rule; queue ids hold queue-specific
    rules. Only enabled rules are exposed.
    """
    config = {}
    rules = (
        CustomFieldRule.objects.filter(enabled=True, field__name__in=field_names)
        .select_related("field")
    )
    for rule in rules:
        entry = config.setdefault(
            rule.field.name, {"display": {}, "required": {}}
        )
        key = WILDCARD_QUEUE if rule.queue_id is None else str(rule.queue_id)
        if rule.display_when:
            entry["display"][key] = rule.display_when
        if rule.required_when:
            entry["required"][key] = rule.required_when
    return config


def _rule_state_for_ticket(rule, context):
    """Visible/required state of one (possibly unsaved) rule in a context."""
    visible = evaluate_predicates(rule.display_when, context)
    if rule.required_when:
        required = visible and evaluate_predicates(rule.required_when, context)
    else:
        required = visible and rule.field.required
    return visible, required


def preview_rule(rule):
    """Classify existing tickets affected by a (possibly unsaved) rule.

    Returns a dict with:

    * ``hidden_with_value``: tickets that currently carry a value for the
      field although the rule would hide it (someone must decide whether
      to keep or clear the old value);
    * ``required_missing``: tickets on which the field would be required
      but no value is stored.

    The rule is never applied here; this is a read-only preview used
    before saving rule changes.
    """
    from helpdesk.models import Ticket

    tickets = (
        Ticket.objects.filter(queue_id=rule.queue_id)
        if rule.queue_id
        else Ticket.objects.all()
    )
    tickets = tickets.prefetch_related("ticketcustomfieldvalue_set__field")

    hidden_with_value = []
    required_missing = []
    total = 0
    for ticket in tickets:
        total += 1
        values = {
            cfv.field.name: cfv.value
            for cfv in ticket.ticketcustomfieldvalue_set.all()
        }
        context = build_context(ticket.queue_id, ticket.priority, values)
        visible, required = _rule_state_for_ticket(rule, context)
        value = values.get(rule.field.name)
        if not visible and value not in (None, ""):
            hidden_with_value.append(ticket)
        if visible and required and _stored_value_is_empty(rule.field, value):
            required_missing.append(ticket)
    return {
        "hidden_with_value": hidden_with_value,
        "required_missing": required_missing,
        "total_scanned": total,
    }
