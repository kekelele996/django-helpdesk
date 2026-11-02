from typing import ClassVar

from django.contrib import admin
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.translation import gettext_lazy as _

from helpdesk import settings as helpdesk_settings
from helpdesk.models import (
    Checklist,
    ChecklistTask,
    ChecklistTemplate,
    CustomField,
    CustomFieldRule,
    EmailTemplate,
    EscalationExclusion,
    FollowUp,
    FollowUpAttachment,
    IgnoreEmail,
    KBIAttachment,
    PreSetReply,
    Queue,
    Ticket,
    TicketChange,
)

if helpdesk_settings.HELPDESK_KB_ENABLED:
    from helpdesk.models import KBCategory, KBItem


@admin.register(Queue)
class QueueAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "email_address", "locale", "time_spent")
    prepopulated_fields: ClassVar[dict] = {"slug": ("title",)}

    def time_spent(self, q):
        if q.dedicated_time:
            return f"{q.time_spent} / {q.dedicated_time}"
        elif q.time_spent:
            return q.time_spent
        else:
            return "-"

    def delete_queryset(self, request, queryset):
        for queue in queryset:
            queue.delete()


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "status",
        "assigned_to",
        "queue",
        "hidden_submitter_email",
        "time_spent",
    )
    date_hierarchy = "created"
    list_filter = ("queue", "assigned_to", "status")
    search_fields = ("id", "title")

    @admin.display(description=_("Submitter E-Mail"))
    def hidden_submitter_email(self, ticket):
        if ticket.submitter_email:
            username, domain = ticket.submitter_email.split("@")
            username = username[:2] + "*" * (len(username) - 2)
            domain = domain[:1] + "*" * (len(domain) - 2) + domain[-1:]
            return f"{username}@{domain}"
        else:
            return ticket.submitter_email

    def time_spent(self, ticket):
        return ticket.time_spent


class TicketChangeInline(admin.StackedInline):
    model = TicketChange
    extra = 0


class FollowUpAttachmentInline(admin.StackedInline):
    model = FollowUpAttachment
    extra = 0


class KBIAttachmentInline(admin.StackedInline):
    model = KBIAttachment
    extra = 0


@admin.register(FollowUp)
class FollowUpAdmin(admin.ModelAdmin):
    inlines: ClassVar[list] = [TicketChangeInline, FollowUpAttachmentInline]
    list_display = (
        "ticket_get_ticket_for_url",
        "title",
        "date",
        "ticket",
        "user",
        "new_status",
        "time_spent",
    )
    list_filter = ("user", "date", "new_status")

    @admin.display(description=_("Slug"))
    def ticket_get_ticket_for_url(self, obj):
        return obj.ticket.ticket_for_url


if helpdesk_settings.HELPDESK_KB_ENABLED:

    @admin.register(KBItem)
    class KBItemAdmin(admin.ModelAdmin):
        list_display = ("category", "title", "last_updated", "team", "order", "enabled")
        inlines: ClassVar[list] = [KBIAttachmentInline]
        readonly_fields = ("voted_by", "downvoted_by")

        list_display_links = ("title",)

    if helpdesk_settings.HELPDESK_KB_ENABLED:

        @admin.register(KBCategory)
        class KBCategoryAdmin(admin.ModelAdmin):
            list_display = ("name", "title", "slug", "public")


class CustomFieldRuleInline(admin.TabularInline):
    model = CustomFieldRule
    extra = 0
    fields = ("queue", "display_when", "required_when", "enabled")


@admin.register(CustomField)
class CustomFieldAdmin(admin.ModelAdmin):
    list_display = ("name", "label", "data_type")
    inlines: ClassVar[list] = [CustomFieldRuleInline]


@admin.register(CustomFieldRule)
class CustomFieldRuleAdmin(admin.ModelAdmin):
    list_display = (
        "field",
        "queue",
        "enabled",
        "display_summary",
        "required_summary",
    )
    list_filter = ("enabled", "queue")
    change_form_template = "helpdesk/admin/customfieldrule_change_form.html"

    @admin.display(description=_("Display conditions"))
    def display_summary(self, obj):
        return len(obj.display_when or [])

    @admin.display(description=_("Required conditions"))
    def required_summary(self, obj):
        return len(obj.required_when or [])

    def get_urls(self):
        custom_urls = [
            path(
                "<path:object_id>/preview/",
                self.admin_site.admin_view(self.preview_saved_view),
                name="helpdesk_customfieldrule_preview",
            ),
        ]
        return custom_urls + super().get_urls()

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # "Preview affected tickets" must show the impact *before* the new
        # rule is saved, so it is intercepted here rather than in
        # response_change(), which runs only after save_model().
        if request.method == "POST" and "_preview" in request.POST:
            return self.preview_unsaved(request, object_id)
        return super().changeform_view(
            request, object_id, form_url, extra_context
        )

    def preview_unsaved(self, request, object_id):
        obj = self.get_object(request, object_id) if object_id else None
        form = self.get_form(request, obj)(
            request.POST, request.FILES, instance=obj
        )
        if not form.is_valid():
            # Hand the errors back through the normal change form.
            return super().changeform_view(request, object_id, "", None)
        rule = form.save(commit=False)
        return self.render_preview(request, rule)

    def preview_saved_view(self, request, object_id):
        rule = get_object_or_404(CustomFieldRule, pk=object_id)
        return self.render_preview(request, rule)

    def render_preview(self, request, rule):
        result = rule.preview_affected_tickets()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "original": rule,
            "rule": rule,
            "result": result,
            "title": _("Preview affected tickets: %s") % rule,
        }
        return TemplateResponse(
            request, "helpdesk/admin/customfieldrule_preview.html", context
        )


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ("template_name", "heading", "locale")
    list_filter = ("locale",)


@admin.register(IgnoreEmail)
class IgnoreEmailAdmin(admin.ModelAdmin):
    list_display = ("name", "queue_list", "email_address", "keep_in_mailbox")


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "task_list")
    search_fields = ("name", "task_list")


class ChecklistTaskInline(admin.TabularInline):
    model = ChecklistTask


@admin.register(Checklist)
class ChecklistAdmin(admin.ModelAdmin):
    list_display = ("name", "ticket")
    search_fields = ("name", "ticket__id", "ticket__title")
    autocomplete_fields = ("ticket",)
    list_select_related = ("ticket",)
    inlines = (ChecklistTaskInline,)


admin.site.register(PreSetReply)
admin.site.register(EscalationExclusion)
