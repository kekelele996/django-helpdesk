"""
views/package.py - staff UI for exporting selected tickets to a verifiable
package and previewing/confirming an import with conflict reporting.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from helpdesk.decorators import helpdesk_staff_member_required
from helpdesk.ticket_package import (
    MAX_PACKAGE_BYTES,
    PackageError,
    export_package,
    import_package,
    load_package,
    prepare_import,
)
from helpdesk.user import HelpdeskUser


SESSION_KEY = "helpdesk_ticket_package_import"


@helpdesk_staff_member_required
@require_POST
def export_tickets_package(request):
    """Download a package containing the tickets checked in the ticket list."""
    ids = request.POST.getlist("ticket_id")
    tickets = HelpdeskUser(request.user).accessible_tickets().filter(id__in=ids)
    if not ids or not tickets.exists():
        messages.error(request, _("Select at least one ticket to export."))
        return redirect("helpdesk:list")

    try:
        data = export_package(tickets)
    except PackageError as exc:
        messages.error(request, str(exc))
        return redirect("helpdesk:list")

    response = HttpResponse(data, content_type="application/zip")
    stamp = timezone.now().strftime("%Y%m%d")
    response["Content-Disposition"] = (
        f'attachment; filename="helpdesk-tickets-{stamp}.zip"'
    )
    response["Content-Length"] = str(len(data))
    return response


def _discard_stash(session):
    stash = session.pop(SESSION_KEY, None)
    if stash:
        try:
            os.remove(stash["path"])
        except OSError:
            pass
        try:
            os.rmdir(os.path.dirname(stash["path"]))
        except OSError:
            pass


def _read_upload(upload):
    content = b""
    for chunk in upload.chunks():
        content += chunk
        if len(content) > MAX_PACKAGE_BYTES:
            raise PackageError(_("Package is larger than the permitted size."))
    return content


@helpdesk_staff_member_required
def ticket_package_import(request):
    huser = HelpdeskUser(request.user)
    accessible_queues = huser.accessible_queues()
    accessible_ids = [q.id for q in accessible_queues]

    if request.method == "POST" and request.POST.get("stage") == "preview":
        upload = request.FILES.get("package")
        if upload is None:
            messages.error(request, _("Choose a package file to upload."))
            return redirect("helpdesk:package_import")
        try:
            content = _read_upload(upload)
            package = load_package(content)
        except PackageError as exc:
            messages.error(request, str(exc))
            return redirect("helpdesk:package_import")

        plan = prepare_import(package, accessible_queue_ids=accessible_ids)
        _discard_stash(request.session)
        directory = tempfile.mkdtemp(prefix="helpdesk-package-")
        path = os.path.join(directory, "package.zip")
        with open(path, "wb") as fh:
            fh.write(content)
        request.session[SESSION_KEY] = {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "name": upload.name,
        }

        return render(
            request,
            "helpdesk/ticket_package_import.html",
            {
                "package": package,
                "plan": plan,
                "queues": accessible_queues,
                "stashed_name": upload.name,
            },
        )

    if request.method == "POST" and request.POST.get("stage") == "confirm":
        stash = request.session.get(SESSION_KEY)
        if not stash:
            messages.error(
                request, _("Your preview expired. Please re-upload the package.")
            )
            return redirect("helpdesk:package_import")
        try:
            with open(stash["path"], "rb") as fh:
                content = fh.read()
            if hashlib.sha256(content).hexdigest() != stash["sha256"]:
                raise PackageError(_("The stashed package changed after preview."))
            package = load_package(content)

            queue_map = {}
            for slug in set(request.POST.getlist("missing_queue_slug")):
                target = request.POST.get(f"queue_{slug}")
                if target:
                    queue_map[slug] = int(target)
            illegal = set(queue_map.values()) - set(accessible_ids)
            if illegal:
                raise PackageError(
                    _("You are not allowed to import into one of the chosen queues.")
                )

            plan = prepare_import(
                package,
                queue_map=queue_map,
                drop_external_refs=bool(request.POST.get("drop_external_refs")),
                accessible_queue_ids=accessible_ids,
            )
        except (PackageError, ValueError, OSError) as exc:
            _discard_stash(request.session)
            messages.error(request, str(exc))
            return redirect("helpdesk:package_import")

        if plan.already_imported:
            _discard_stash(request.session)
            messages.info(
                request,
                _(
                    "This package has already been imported; no duplicate "
                    "tickets were created."
                ),
            )
            return redirect("helpdesk:list")

        if not plan.ready:
            # Keep the stash so the user can adjust the choices and retry.
            return render(
                request,
                "helpdesk/ticket_package_import.html",
                {
                    "package": package,
                    "plan": plan,
                    "queues": accessible_queues,
                    "stashed_name": stash.get("name"),
                },
            )

        try:
            created = import_package(package, plan)
        except PackageError as exc:
            messages.error(request, str(exc))
            return redirect("helpdesk:package_import")
        finally:
            _discard_stash(request.session)

        messages.success(
            request,
            _("%(count)d tickets imported successfully as a single batch.")
            % {"count": len(created)},
        )
        return redirect("helpdesk:list")

    return render(
        request,
        "helpdesk/ticket_package_import.html",
        {"queues": accessible_queues},
    )
