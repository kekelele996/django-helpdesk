Custom Fields
=============

django-helpdesk supports custom fields on the ``Ticket`` model. These fields are created by using the Django administration tool, and are shown on both the public and staff submission forms. You can use most Django field types including text, integer, boolean, and list.

Custom fields are relatively inefficient; you can search them, but this might degrade performance of your installation if you make use of custom fields. They can be useful for tracking extra information that your organisation needs but that isn't supported out of the box.

Conditional display and required rules
--------------------------------------

In the Django administration you can attach *custom field rules* to a field (also editable inline on the custom field itself). A rule is scoped to one queue, or to every queue when the queue is left blank; a queue-specific rule overrides the global rule for that field.

Each rule holds two predicate lists as JSON, ``display_when`` and ``required_when``. All predicates in a list must match (AND); an empty list means "always". A predicate references ``queue``, ``priority`` or another ``custom_<name>`` field and supports the operators ``eq``, ``ne``, ``in``, ``not_in``, ``contains``, ``empty`` and ``filled``::

    [{"field": "custom_fault_type", "operator": "eq", "value": "network"}]

The same rules are enforced on the public submission form, staff ticket creation, the ticket edit and respond forms, the REST API and tickets created from incoming e-mail, so validation cannot drift between entry points. The forms hide and mark fields client-side, but the rules are always re-evaluated on the server.

Editing a rule never changes existing tickets. Before saving, use the *Preview affected tickets* button on the rule to see tickets that still carry a value although the field would be hidden, and tickets on which the field would become required while empty. When such a hidden field has a stored value, the ticket edit form asks whether to keep or clear it; clearing requires an explicit confirmation and is recorded as a ticket change, so the removed value stays findable in the ticket history and history never shows the field as having been empty all along. E-mail-created tickets cannot provide custom field values, so a private follow-up is added listing the required information that is still missing.
