"""
ticket_package.py - Verifiable export/import of tickets between environments.

A ticket package is a ZIP archive holding:

  manifest.json - envelope with the SHA-256 digests of the payload and files
  tickets.json  - tickets with follow-ups, changes, CC followers, custom field
                  values and dependency/merge relations
  attachments/  - one file per follow-up attachment

Design goals (see requirements):

* Only whitelisted content is ever serialized. Mailbox credentials, proxy and
  logging settings, ticket secret keys, absolute server URLs and user accounts
  are never written into a package. Users are referenced by e-mail address
  only, queues by their slug.
* Import is all-or-nothing: a single database transaction plus cleanup of any
  storage writes performed before a failure.
* Importing the same package twice never creates duplicates: the package id
  together with package-local ticket references is recorded in
  ``TicketPackageImport``, so repeated imports return the existing tickets.
* Tickets are recreated with fresh ids in dependency order, and no e-mail or
  webhook notification is emitted while doing so.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import posixpath
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from types import SimpleNamespace
from zipfile import ZipFile

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from helpdesk.models import (
    CustomField,
    FollowUp,
    FollowUpAttachment,
    Queue,
    Ticket,
    TicketCC,
    TicketChange,
    TicketCustomFieldValue,
    TicketDependency,
    TicketPackageImport,
    User,
)
from helpdesk.validators import validate_file_extension


PACKAGE_FORMAT = "django-helpdesk-ticket-package"
PACKAGE_VERSION = 1
MANIFEST_NAME = "manifest.json"
PAYLOAD_NAME = "tickets.json"
ATTACHMENTS_PREFIX = "attachments/"
#: Reject absurdly large uploads / expanded attachment payloads.
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 512 * 1024 * 1024
#: Fields copied verbatim from a Ticket, nothing else.
TICKET_FIELDS = (
    "title",
    "submitter_email",
    "status",
    "on_hold",
    "description",
    "resolution",
    "priority",
)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class PackageError(Exception):
    """A package is missing data, malformed, fails verification or cannot be
    imported in the requested state."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(value):
    return value.isoformat() if value is not None else None


def _parse_dt(value):
    if value is None:
        return None
    parsed = timezone.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


def _parse_duration(value):
    if value is None:
        return None
    return datetime.timedelta(seconds=float(value))


def _safe_basename(name: str) -> str:
    """Reduce an arbitrary file name to a single safe path component."""
    name = posixpath.basename(name.replace("\\", "/"))
    name = _SAFE_NAME_RE.sub("_", name).strip("._")
    return name[:180] or "attachment"


def _topological_order(refs, prerequisites):
    """Return refs with every prerequisite first; deterministic on ties.

    :param refs: iterable of node identifiers
    :param prerequisites: mapping node -> set of nodes that must come first
    :raises PackageError: if the graph contains a cycle
    """
    remaining = {r: set(p) for r, p in prerequisites.items()}
    ordered = []
    while len(ordered) < len(remaining):
        ready = sorted(
            r
            for r in remaining
            if r not in ordered and not remaining[r] - set(ordered)
        )
        if not ready:
            stuck = sorted(set(remaining) - set(ordered))
            raise PackageError(
                _("Circular ticket dependency involving: %s") % ", ".join(stuck)
            )
        ordered.extend(ready)
    return ordered


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def _serialize_ticket(ticket, selected_ids, files, used_names):
    depends_on = []
    depends_on_external = []
    for dep in ticket.ticketdependency.all():
        if dep.depends_on_id in selected_ids:
            depends_on.append(str(dep.depends_on_id))
        else:
            depends_on_external.append(dep.depends_on_id)

    merged_to = None
    merged_to_external = None
    if ticket.merged_to_id:
        if ticket.merged_to_id in selected_ids:
            merged_to = str(ticket.merged_to_id)
        else:
            merged_to_external = ticket.merged_to_id

    followups = []
    for fup in (
        ticket.followup_set.all()
        .order_by("date", "id")
        .prefetch_related("ticketchange_set")
    ):
        attachments = []
        for att in fup.followupattachment_set.all().order_by("id"):
            with att.file.open("rb") as fh:
                content = fh.read()
            basename = _safe_basename(att.file.name)
            arcname = f"{ATTACHMENTS_PREFIX}{len(files) + 1:06d}-{basename}"
            while arcname in used_names:
                arcname = (
                    f"{ATTACHMENTS_PREFIX}{len(files) + 1:06d}-"
                    f"{uuid.uuid4().hex[:8]}-{basename}"
                )
            used_names.add(arcname)
            files[arcname] = content
            attachments.append(
                {
                    "arcname": arcname,
                    "filename": basename,
                    "mime_type": att.mime_type,
                    "size": len(content),
                    "sha256": _sha256(content),
                }
            )

        followups.append(
            {
                "date": _iso(fup.date),
                "title": fup.title,
                "comment": fup.comment,
                "public": bool(fup.public),
                "new_status": fup.new_status,
                "time_spent_seconds": (
                    fup.time_spent.total_seconds()
                    if fup.time_spent is not None
                    else None
                ),
                "user_email": fup.user.email if fup.user_id and fup.user.email else None,
                "changes": [
                    {
                        "field": change.field,
                        "old_value": change.old_value,
                        "new_value": change.new_value,
                    }
                    for change in fup.ticketchange_set.all().order_by("id")
                ],
                "attachments": attachments,
            }
        )

    data = {
        "ref": str(ticket.id),
        "source_id": ticket.id,
        "queue_slug": ticket.queue.slug,
        "assigned_to_email": (
            ticket.assigned_to.email
            if ticket.assigned_to_id and ticket.assigned_to.email
            else None
        ),
        "custom_fields": [
            {"name": cfv.field.name, "value": cfv.value}
            for cfv in ticket.ticketcustomfieldvalue_set.select_related("field")
        ],
        "ccs": [
            {
                "user_email": cc.user.email if cc.user_id and cc.user.email else None,
                "email": cc.email,
                "can_view": bool(cc.can_view),
                "can_update": bool(cc.can_update),
            }
            for cc in ticket.ticketcc_set.all()
        ],
        "followups": followups,
        "depends_on": sorted(depends_on, key=int),
        "depends_on_external": sorted(depends_on_external),
        "merged_to": merged_to,
        "merged_to_external": merged_to_external,
        "due_date": _iso(ticket.due_date),
        "created": _iso(ticket.created),
    }
    for field_name in TICKET_FIELDS:
        data[field_name] = getattr(ticket, field_name)
    return data


def export_package(tickets) -> bytes:
    """Build a verifiable package (ZIP bytes) for the given tickets.

    Relations to tickets outside the selection are recorded as external
    references so the importer can report them instead of silently dropping
    them.
    """
    tickets = list(
        tickets.select_related("queue", "assigned_to")
        .prefetch_related(
            "ticketdependency",
            "followup_set__followupattachment_set",
            "ticketcc_set__user",
            "ticketcustomfieldvalue_set__field",
        )
        .order_by("id")
    )
    if not tickets:
        raise PackageError(_("No tickets selected for export."))

    selected_ids = {t.id for t in tickets}
    prerequisites = {
        str(t.id): {
            str(d.depends_on_id)
            for d in t.ticketdependency.all()
            if d.depends_on_id in selected_ids
        }
        for t in tickets
    }
    order = _topological_order([str(t.id) for t in tickets], prerequisites)
    by_id = {str(t.id): t for t in tickets}

    files: dict[str, bytes] = {}
    used_names: set[str] = set()
    payload_tickets = [
        _serialize_ticket(by_id[ref], selected_ids, files, used_names)
        for ref in order
    ]

    package_id = uuid.uuid4()
    exported_at = timezone.now()
    payload = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "package_id": str(package_id),
        "exported_at": _iso(exported_at),
        "tickets": payload_tickets,
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    manifest = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "package_id": str(package_id),
        "exported_at": _iso(exported_at),
        "payload_sha256": _sha256(payload_bytes),
        "tickets_count": len(payload_tickets),
        "files": {
            arcname: {"sha256": _sha256(content), "size": len(content)}
            for arcname, content in sorted(files.items())
        },
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")

    buffer = io.BytesIO()
    with ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME, manifest_bytes)
        archive.writestr(PAYLOAD_NAME, payload_bytes)
        for arcname, content in files.items():
            archive.writestr(arcname, content)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Loading / verification
# --------------------------------------------------------------------------- #


@dataclass
class LoadedPackage:
    package_id: uuid.UUID
    exported_at: str
    payload: dict
    files: dict = field(default_factory=dict)  # arcname -> bytes

    @property
    def tickets(self):
        return self.payload["tickets"]

    def ticket_by_ref(self, ref):
        return self._by_ref[ref]

    def __post_init__(self):
        self._by_ref = {t["ref"]: t for t in self.tickets}


def _is_str(v):
    return isinstance(v, str)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_followup(fup, fwhere, errors):
    if not isinstance(fup, dict):
        errors.append(_("%s: not an object") % fwhere)
        return
    if not _is_str(fup.get("date")):
        errors.append(_("%s: date must be a string") % fwhere)
    for name in ("title", "comment"):
        if fup.get(name) is not None and not _is_str(fup.get(name)):
            errors.append(_("%s: %s must be a string or null") % (fwhere, name))
    if not isinstance(fup.get("public"), bool):
        errors.append(_("%s: public must be a boolean") % fwhere)
    new_status = fup.get("new_status")
    if new_status is not None and not _is_int(new_status):
        errors.append(_("%s: new_status must be an integer or null") % fwhere)
    spent = fup.get("time_spent_seconds")
    if spent is not None and not isinstance(spent, (int, float)):
        errors.append(_("%s: time_spent_seconds must be a number") % fwhere)
    if not isinstance(fup.get("changes", []), list):
        errors.append(_("%s: changes must be a list") % fwhere)
    atts = fup.get("attachments", [])
    if not isinstance(atts, list):
        errors.append(_("%s: attachments must be a list") % fwhere)
        return
    for att in atts:
        if not isinstance(att, dict) or not _is_str(att.get("arcname")):
            errors.append(_("%s: attachment is malformed") % fwhere)


def _validate_ticket_fields(t, where, errors):
    for name in ("queue_slug", "title"):
        if not _is_str(t.get(name)):
            errors.append(_("%s: %s must be a string") % (where, name))
    for name in ("status", "priority", "source_id"):
        if not _is_int(t.get(name)):
            errors.append(_("%s: %s must be an integer") % (where, name))
    if not isinstance(t.get("on_hold"), bool):
        errors.append(_("%s: on_hold must be a boolean") % where)
    for name in ("submitter_email", "description", "resolution", "created"):
        if t.get(name) is not None and not _is_str(t.get(name)):
            errors.append(_("%s: %s must be a string or null") % (where, name))
    if not isinstance(t.get("custom_fields", []), list):
        errors.append(_("%s: custom_fields must be a list") % where)
    if not isinstance(t.get("ccs", []), list):
        errors.append(_("%s: ccs must be a list") % where)


def _validate_structure(payload, errors):
    tickets = payload.get("tickets")
    if not isinstance(tickets, list) or not tickets:
        errors.append(_("Package contains no tickets."))
        return

    seen = set()
    for i, t in enumerate(tickets):
        where = _("ticket #%d") % i
        if not isinstance(t, dict):
            errors.append(_("%s: not an object") % where)
            continue
        ref = t.get("ref")
        if not _is_str(ref) or not ref:
            errors.append(_("%s: missing reference") % where)
        elif ref in seen:
            errors.append(_("%s: duplicate ticket reference %s") % (where, ref))
        seen.add(ref)

        _validate_ticket_fields(t, where, errors)

        followups = t.get("followups", [])
        if not isinstance(followups, list):
            errors.append(_("%s: followups must be a list") % where)
            followups = []
        for j, fup in enumerate(followups):
            fwhere = _("%(ticket)s follow-up #%(n)d") % {"ticket": where, "n": j}
            _validate_followup(fup, fwhere, errors)

        deps = t.get("depends_on", [])
        if not isinstance(deps, list) or not all(_is_str(v) for v in deps):
            errors.append(_("%s: depends_on must be a list of references") % where)
        ext = t.get("depends_on_external", [])
        if not isinstance(ext, list) or not all(_is_int(v) for v in ext):
            errors.append(_("%s: depends_on_external must be a list of ids") % where)
        merged = t.get("merged_to")
        if merged is not None and not _is_str(merged):
            errors.append(_("%s: merged_to must be a reference or null") % where)
        merged_ext = t.get("merged_to_external")
        if merged_ext is not None and not _is_int(merged_ext):
            errors.append(_("%s: merged_to_external must be an id or null") % where)


def _safe_arcname(name):
    if not name or name.endswith("/"):
        return False
    if name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    if any(part in ("", "..") for part in parts):
        return False
    if parts[0] == "attachments" and len(parts) == 2:
        return True
    return name in (MANIFEST_NAME, PAYLOAD_NAME)


def _check_internal_refs(payload):
    refs = {t["ref"] for t in payload["tickets"]}
    for t in payload["tickets"]:
        for ref in t.get("depends_on", []):
            if ref not in refs:
                raise PackageError(
                    _("Ticket %s depends on a ticket missing from the package: %s")
                    % (t["ref"], ref)
                )
        if t.get("merged_to") and t["merged_to"] not in refs:
            raise PackageError(
                _("Ticket %s merges into a ticket missing from the package: %s")
                % (t["ref"], t["merged_to"])
            )


def _verify_attachments(archive, manifest, payload, names, errors):
    """Cross-check attachment references, declarations and stored files."""
    declared = manifest.get("files")
    if not isinstance(declared, dict):
        errors.append(_("manifest.json is missing the file digest list."))
        declared = {}

    referenced = set()
    for t in payload.get("tickets", []):
        for fup in t.get("followups", []):
            for att in fup.get("attachments", []):
                referenced.add(att.get("arcname"))

    in_zip = {n for n in names if n.startswith(ATTACHMENTS_PREFIX)}
    for name in referenced - set(declared):
        errors.append(_("Attachment not listed in manifest: %s") % name)
    for name in set(declared) - in_zip:
        errors.append(_("Manifest lists an attachment missing from package: %s") % name)
    for name in in_zip - referenced:
        errors.append(_("Package contains an undeclared attachment: %s") % name)
    for name in referenced - in_zip:
        errors.append(_("Attachment listed but missing from package: %s") % name)

    files = {}
    total = 0
    for arcname in sorted(referenced & in_zip):
        meta = declared.get(arcname) or {}
        content = archive.read(arcname)  # raises on CRC mismatch
        total += len(content)
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise PackageError(_("Package attachments exceed the size limit."))
        if _is_int(meta.get("size")) and meta.get("size") != len(content):
            errors.append(_("Attachment size mismatch: %s") % arcname)
        digest = meta.get("sha256")
        if not _is_str(digest) or _sha256(content) != digest:
            errors.append(_("Attachment checksum mismatch: %s") % arcname)
        files[arcname] = content
    return files


def _read_envelope(archive):
    """Validate manifest header and payload digest; return the parts."""
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        payload_bytes = archive.read(PAYLOAD_NAME)
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(_("Package metadata cannot be read: %s") % exc) from exc

    if not isinstance(manifest, dict):
        raise PackageError(_("manifest.json is malformed."))
    if manifest.get("format") != PACKAGE_FORMAT:
        raise PackageError(_("Unsupported package format."))
    if manifest.get("version") != PACKAGE_VERSION:
        raise PackageError(
            _("Unsupported package version: %s") % manifest.get("version")
        )
    try:
        package_id = uuid.UUID(str(manifest.get("package_id")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise PackageError(_("Package id is not valid.")) from exc

    if manifest.get("payload_sha256") != _sha256(payload_bytes):
        raise PackageError(
            _(
                "Package verification failed: tickets.json does not match "
                "its checksum. The file may be corrupt or tampered with."
            )
        )

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(_("tickets.json is not valid JSON: %s") % exc) from exc

    if payload.get("package_id") != str(package_id):
        raise PackageError(_("Package id mismatch between manifest and payload."))
    if (
        payload.get("format") != PACKAGE_FORMAT
        or payload.get("version") != PACKAGE_VERSION
    ):
        raise PackageError(_("Unsupported payload format or version."))
    return manifest, payload, package_id


def load_package(data) -> LoadedPackage:
    """Parse and verify a package, returning its contents ready for a plan."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise PackageError(_("Package must be a ZIP archive."))
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError(_("Package is larger than the permitted size."))

    try:
        archive = ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackageError(_("Not a valid ZIP archive: %s") % exc) from exc

    with archive:
        names = [n for n in archive.namelist() if not n.endswith("/")]
        for name in names:
            if not _safe_arcname(name):
                raise PackageError(
                    _("Unsafe or unexpected path in package: %s") % name
                )
        if MANIFEST_NAME not in names or PAYLOAD_NAME not in names:
            raise PackageError(_("Package is missing manifest.json or tickets.json."))

        manifest, payload, package_id = _read_envelope(archive)
        errors = []
        _validate_structure(payload, errors)
        files = _verify_attachments(archive, manifest, payload, names, errors)
        if errors:
            raise PackageError("; ".join(str(e) for e in errors))

    _check_internal_refs(payload)

    return LoadedPackage(
        package_id=package_id,
        exported_at=str(manifest.get("exported_at", "")),
        payload=payload,
        files=files,
    )


# --------------------------------------------------------------------------- #
# Import planning
# --------------------------------------------------------------------------- #
@dataclass
class TicketPlan:
    ref: str
    source_id: int
    title: str
    queue_slug: str
    queue: Queue | None = None
    missing_custom_fields: list = field(default_factory=list)
    missing_user_emails: list = field(default_factory=list)


@dataclass
class ImportPlan:
    package: LoadedPackage
    errors: list = field(default_factory=list)
    missing_references: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    tickets: list = field(default_factory=list)  # TicketPlan in creation order
    missing_queue_slugs: list = field(default_factory=list)
    already_imported: dict = field(default_factory=dict)
    drop_external_refs: bool = False

    @property
    def ready(self):
        return not self.errors


def _resolve_users(emails):
    users = {
        u.email: u
        for u in User.objects.filter(email__in=sorted({e for e in emails if e}))
    }
    return users


def prepare_import(
    package: LoadedPackage,
    queue_map=None,
    *,
    drop_external_refs=False,
    accessible_queue_ids=None,
) -> ImportPlan:
    """Build an import plan describing conflicts and missing references.

    :param queue_map: optional mapping source queue slug -> target Queue id,
                      used to remap queues absent under their source slug.
    :param drop_external_refs: when True, dependencies on / merges into tickets
                      not contained in the package are dropped instead of
                      blocking the import.
    :param accessible_queue_ids: when provided, target queues must belong to
                      this set (permission boundary enforced by the view).
    """
    queue_map = queue_map or {}
    plan = ImportPlan(package=package, drop_external_refs=drop_external_refs)

    existing = {
        record.local_ref: record.ticket
        for record in TicketPackageImport.objects.filter(
            package_id=package.package_id
        ).select_related("ticket")
    }
    refs = {t["ref"] for t in package.tickets}
    if set(existing) == refs:
        plan.already_imported = existing
        return plan
    # Queues ---------------------------------------------------------------- #
    queue_slugs = {t["queue_slug"] for t in package.tickets}
    queues = {q.slug: q for q in Queue.objects.filter(slug__in=queue_slugs)}
    mapped_queues = {}
    for qid in queue_map.values():
        if qid is not None and qid not in mapped_queues:
            mapped_queues[qid] = Queue.objects.filter(id=qid).first()
    # Users ----------------------------------------------------------------- #
    emails = set()
    for t in package.tickets:
        emails.add(t.get("assigned_to_email"))
        for cc in t.get("ccs", []):
            emails.add(cc.get("user_email"))
        for fup in t.get("followups", []):
            emails.add(fup.get("user_email"))
    users = _resolve_users(emails)

    custom_fields = {f.name for f in CustomField.objects.all()}

    for t in package.tickets:
        tp = TicketPlan(
            ref=t["ref"],
            source_id=t["source_id"],
            title=t["title"],
            queue_slug=t["queue_slug"],
        )
        queue = queues.get(t["queue_slug"])
        if queue is None:
            target_id = queue_map.get(t["queue_slug"])
            queue = mapped_queues.get(target_id) if target_id is not None else None
            if queue is None:
                plan.missing_queue_slugs.append(t["queue_slug"])
                plan.errors.append(
                    _(
                        "Ticket %(ref)s needs queue '%(slug)s', which does not "
                        "exist in this environment."
                    )
                    % {"ref": t["ref"], "slug": t["queue_slug"]}
                )
        if (
            queue is not None
            and accessible_queue_ids is not None
            and queue.id not in accessible_queue_ids
        ):
            plan.errors.append(
                _("You are not allowed to import into queue '%s'.") % queue.slug
            )
        tp.queue = queue

        missing_cf = [
            cf["name"]
            for cf in t.get("custom_fields", [])
            if cf["name"] not in custom_fields
        ]
        if missing_cf:
            tp.missing_custom_fields = missing_cf
            plan.warnings.append(
                _("Ticket %s: unknown custom fields skipped (%s).")
                % (t["ref"], ", ".join(sorted(set(missing_cf))))
            )

        for _role, email in (
            (_("owner"), t.get("assigned_to_email")),
            *[
                (_("follow-up author"), fup.get("user_email"))
                for fup in t.get("followups", [])
            ],
        ):
            if email and email not in users:
                tp.missing_user_emails.append(email)
        missing_users = sorted({e for e in tp.missing_user_emails if e})
        for email in missing_users:
            plan.warnings.append(
                _("Ticket %(ref)s: %(what)s '%(email)s' has no account here; "
                  "the reference will be left empty.")
                % {"ref": t["ref"], "what": _("user"), "email": email}
            )

        for ext in t.get("depends_on_external", []):
            msg = _(
                "Ticket %(ref)s depends on source ticket #%(id)s, which was "
                "not included in the package."
            ) % {"ref": t["ref"], "id": ext}
            plan.missing_references.append(msg)
            if not drop_external_refs:
                plan.errors.append(msg)
        if t.get("merged_to_external"):
            msg = _(
                "Ticket %(ref)s was merged into source ticket #%(id)s, which "
                "was not included in the package."
            ) % {"ref": t["ref"], "id": t["merged_to_external"]}
            plan.missing_references.append(msg)
            if not drop_external_refs:
                plan.errors.append(msg)

        plan.tickets.append(tp)

    plan.missing_queue_slugs = sorted(set(plan.missing_queue_slugs))
    # Internal dependency graph: fail fast on a cycle and order the work.
    prerequisites = {
        t["ref"]: set(t.get("depends_on", [])) for t in package.tickets
    }
    order = _topological_order(list(prerequisites), prerequisites)
    plan.tickets.sort(key=lambda tp: order.index(tp.ref))
    return plan


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #


def _import_followers(ticket, ccs, users):
    for cc in ccs:
        user = users.get(cc.get("user_email"))
        # A follower that was a user account in the source becomes a plain
        # e-mail follower if that account is absent here.
        email = None if user else (cc.get("email") or cc.get("user_email"))
        if user is None and not email:
            continue
        TicketCC.objects.create(
            ticket=ticket,
            user=user,
            email=email,
            can_view=bool(cc.get("can_view")),
            can_update=bool(cc.get("can_update")),
        )


def _import_attachment(followup, att_data, content, saved_files):
    filename = att_data["filename"]
    validate_file_extension(SimpleNamespace(name=filename))
    attachment = FollowUpAttachment(
        followup=followup,
        filename=filename,
        mime_type=att_data.get("mime_type") or "application/octet-stream",
        size=len(content),
    )
    attachment.file.save(filename, ContentFile(content), save=True)
    saved_files.append((attachment.file.storage, attachment.file.name))


def _import_followup(ticket, fup_data, users, package, saved_files):
    followup = FollowUp.objects.create(
        ticket=ticket,
        date=_parse_dt(fup_data["date"]),
        title=fup_data.get("title"),
        comment=fup_data.get("comment"),
        public=bool(fup_data.get("public")),
        new_status=fup_data.get("new_status"),
        user=users.get(fup_data.get("user_email")),
        message_id=None,
        email_recipients=None,
    )
    # Replaying history is not new billable work: keep the original time
    # spent instead of the auto-calculated one.
    FollowUp.objects.filter(pk=followup.pk).update(
        time_spent=_parse_duration(fup_data.get("time_spent_seconds"))
    )

    changes = [
        TicketChange(
            followup=followup,
            field=change["field"],
            old_value=change.get("old_value"),
            new_value=change.get("new_value"),
        )
        for change in fup_data.get("changes", [])
    ]
    if changes:
        TicketChange.objects.bulk_create(changes)

    for att_data in fup_data.get("attachments", []):
        _import_attachment(
            followup, att_data, package.files[att_data["arcname"]], saved_files
        )


def _import_ticket(tp, data, queue, users, custom_fields, package, saved_files):
    ticket = Ticket(
        title=data["title"],
        queue=queue,
        submitter_email=data.get("submitter_email") or None,
        status=data["status"],
        on_hold=data["on_hold"],
        description=data.get("description"),
        resolution=data.get("resolution"),
        priority=data["priority"],
        due_date=_parse_dt(data.get("due_date")),
        assigned_to=users.get(data.get("assigned_to_email")),
        kbitem=None,
    )
    ticket.save()
    # auto_now_add would otherwise reset the original creation date
    created = _parse_dt(data.get("created"))
    if created is not None:
        Ticket.objects.filter(pk=ticket.pk).update(created=created)
        ticket.created = created

    for cf in data.get("custom_fields", []):
        field = custom_fields.get(cf["name"])
        if field is not None:
            TicketCustomFieldValue.objects.create(
                ticket=ticket, field=field, value=cf.get("value")
            )

    _import_followers(ticket, data.get("ccs", []), users)

    for fup_data in data.get("followups", []):
        _import_followup(ticket, fup_data, users, package, saved_files)

    return ticket


def import_package(package: LoadedPackage, plan: ImportPlan):
    """Create the planned tickets. Returns the new (or existing) tickets.

    Either every ticket is created or nothing is committed. Files written to
    storage before a failure are deleted afterwards.
    """
    if plan.already_imported:
        return list(plan.already_imported.values())
    if plan.errors:
        raise PackageError(
            _("Import refused while conflicts or missing references remain.")
        )

    queue_by_ref = {tp.ref: tp.queue for tp in plan.tickets}

    emails = set()
    for t in package.tickets:
        emails.add(t.get("assigned_to_email"))
        for cc in t.get("ccs", []):
            emails.add(cc.get("user_email"))
        for fup in t.get("followups", []):
            emails.add(fup.get("user_email"))
    users = _resolve_users(emails)
    custom_fields = {f.name: f for f in CustomField.objects.all()}

    new_tickets = {}
    saved_files = []
    mappings = []

    try:
        with transaction.atomic():
            for tp in plan.tickets:
                data = package.ticket_by_ref(tp.ref)
                new_tickets[tp.ref] = _import_ticket(
                    tp,
                    data,
                    queue_by_ref[tp.ref],
                    users,
                    custom_fields,
                    package,
                    saved_files,
                )
                mappings.append(
                    TicketPackageImport(
                        package_id=package.package_id,
                        local_ref=tp.ref,
                        ticket=new_tickets[tp.ref],
                    )
                )
            # Relations are wired only after every ticket exists, in the
            # dependency order computed by the plan.
            for tp in plan.tickets:
                data = package.ticket_by_ref(tp.ref)
                for ref in data.get("depends_on", []):
                    TicketDependency.objects.create(
                        ticket=new_tickets[tp.ref], depends_on=new_tickets[ref]
                    )
                if data.get("merged_to"):
                    new_tickets[tp.ref].merged_to = new_tickets[data["merged_to"]]
                    new_tickets[tp.ref].save(update_fields=["merged_to"])

            TicketPackageImport.objects.bulk_create(mappings)
    except Exception:
        # Database objects are rolled back by the transaction; storage writes
        # are not, so remove them explicitly.
        for storage, name in saved_files:
            try:
                storage.delete(name)
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        raise

    return [new_tickets[tp.ref] for tp in plan.tickets]
