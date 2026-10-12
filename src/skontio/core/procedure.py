"""Verfahrensdokumentation (GoBD Rz. 151 ff.): generated from the running configuration.

Each version is rendered as PDF/A, stored in the write-once archive with a 10-year retention and
listed with the reason for the change, so earlier versions stay readable (GoBD: Historisierung).
The generated text covers what Skontio does; company-specific processes outside Skontio (paper
receipts, cash, bank) are marked for the user to add.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.db.models import Count, Max
from django.template.loader import render_to_string
from django.utils import timezone
from oauth2_provider.models import Application

from skontio import __version__
from skontio.accounts.models import OidcIdentity, TotpDevice
from skontio.archive.models import Blob
from skontio.archive.services import retention_end, store
from skontio.audit.models import AuditEvent
from skontio.audit.services import record, verify_chain
from skontio.core.models import Company, ProcedureDocument
from skontio.documents.models import Document, DocumentTemplate
from skontio.documents.renditions import INVOICE_RETENTION_YEARS, LETTER_RETENTION_YEARS
from skontio.mail.services import smtp_configured
from skontio.mcp.tokens import active_tokens
from skontio.numbering.models import NumberSequence
from skontio.ops.backup import BACKUP_NAME
from skontio.rendering.pdf import render_html_pdf_a3
from skontio.taxstatus.models import TaxStatusPeriod

PROCEDURE_RETENTION_YEARS = 10  # Organisationsunterlagen, § 147 Abs. 1 Nr. 1, Abs. 3 AO
SEQUENCE_LABELS = dict(Document.Type.choices)
REGIME_LABELS = {"small_business": "Kleinunternehmer (§ 19 UStG)", "standard": "Regelbesteuerung"}


class ProcedureError(ValueError):
    pass


def _users() -> list[dict]:
    totp = set(TotpDevice.objects.filter(confirmed_at__isnull=False).values_list("user_id", flat=True))
    oidc = set(OidcIdentity.objects.values_list("user_id", flat=True))
    return [
        {
            "username": user.get_username(),
            "role": "Administrator" if user.is_staff else "Benutzer",
            "active": user.is_active,
            "oidc": user.pk in oidc,
            "totp": user.pk in totp,
            "last_login": user.last_login,
        }
        for user in get_user_model().objects.order_by("username")
    ]


def _latest_backup() -> dict | None:
    directory = Path(settings.BACKUP_DIR)
    if not directory.is_dir():
        return None
    backups = sorted(p for p in directory.iterdir() if p.is_file() and BACKUP_NAME.match(p.name))
    if not backups:
        return None
    latest = backups[-1]
    taken = dt.datetime.fromtimestamp(latest.stat().st_mtime, tz=dt.UTC)
    return {"name": latest.name, "taken": timezone.localtime(taken), "count": len(backups)}


def procedure_context(company: Company) -> dict:
    today = timezone.localdate()
    finalized = (
        Document.objects.filter(company=company, status=Document.Status.FINALIZED)
        .values("doc_type")
        .annotate(count=Count("pk"), last=Max("issue_date"))
        .order_by("doc_type")
    )
    return {
        "company": company,
        "today": today,
        "version_info": __version__,
        "database": {"sqlite": "SQLite", "postgresql": "PostgreSQL"}.get(connection.vendor, connection.vendor),
        "time_zone": settings.TIME_ZONE,
        "public_url": settings.PUBLIC_URL,
        "oidc_issuer": settings.OIDC_ISSUER,
        "local_login_networks": settings.LOCAL_LOGIN_NETWORKS,
        "local_login_2fa": settings.LOCAL_LOGIN_REQUIRE_2FA,
        "users": _users(),
        "sequences": [
            {"label": SEQUENCE_LABELS.get(s.doc_type, s.doc_type), "pattern": s.pattern, "reset": s.get_reset_display()}
            for s in NumberSequence.objects.filter(company=company).order_by("doc_type")
        ],
        "finalized": [
            {"label": SEQUENCE_LABELS.get(row["doc_type"], row["doc_type"]), "count": row["count"], "last": row["last"]}
            for row in finalized
        ],
        "periods": [
            {"period": p, "regime": REGIME_LABELS.get(p.regime, p.regime)}
            for p in TaxStatusPeriod.objects.filter(company=company).select_related("confirmed_by")
        ],
        "templates": DocumentTemplate.objects.filter(company=company).count(),
        "invoice_years": INVOICE_RETENTION_YEARS,
        "letter_years": LETTER_RETENTION_YEARS,
        "procedure_years": PROCEDURE_RETENTION_YEARS,
        "archive_blobs": Blob.objects.count(),
        "legal_holds": Blob.objects.filter(legal_hold=True).count(),
        "backup_dir": settings.BACKUP_DIR,
        "backup_keep": settings.BACKUP_KEEP,
        "latest_backup": _latest_backup(),
        "smtp": smtp_configured(),
        "audit_events": AuditEvent.objects.count(),
        "audit_problems": verify_chain(),
        "mcp_pseudonymise": settings.MCP_PSEUDONYMISE_PERSONS,
        "mcp_clients": Application.objects.filter(accesstoken__isnull=False).distinct().count(),
        "mcp_tokens": list(active_tokens().select_related("user")),
        "versions": ProcedureDocument.objects.filter(company=company).order_by("version"),
    }


def render_procedure_html(company: Company, *, version: int, note: str) -> str:
    return render_to_string("documents/procedure.html", procedure_context(company) | {"version": version, "note": note})


def generate(company: Company, *, note: str, user=None) -> ProcedureDocument:
    """Render, archive and register a new version."""
    note = note.strip()[:300]
    if not note:
        raise ProcedureError("Bitte den Anlass angeben (z. B. „Ersterstellung“ oder „Wechsel zur Regelbesteuerung“).")
    with transaction.atomic():
        last = ProcedureDocument.objects.select_for_update().filter(company=company).order_by("-version").first()
        version = (last.version + 1) if last else 1
        pdf = render_html_pdf_a3(render_procedure_html(company, version=version, note=note))
        blob = store(
            pdf,
            mime="application/pdf",
            retention_until=retention_end(timezone.localdate(), PROCEDURE_RETENTION_YEARS),
        )
        document = ProcedureDocument.objects.create(
            company=company, version=version, blob=blob, note=note, created_by=user
        )
        record(
            "procedure.generate",
            actor=user,
            company=company,
            entity="procedure_document",
            entity_id=document.pk,
            data={"version": version, "note": note, "sha256": blob.sha256},
        )
    return document
