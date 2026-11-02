"""Werkzeuge für den Alltag mit KI: Entwürfe überblicken und verwerfen, offene Bankumsätze sehen.

Bewusst nicht dabei: festschreiben, versenden, Zahlungen buchen, Firmendaten ändern – das bleibt bei Menschen.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from skontio.audit.services import record
from skontio.banking.models import BankTransaction
from skontio.banking.services import suggestions
from skontio.documents.models import Document
from skontio.tools.inputs import Input, Limit
from skontio.tools.registry import Out, ToolError, tool
from skontio.tools.views import customer_view, number

AI_ORIGINS = {Document.Origin.MCP, Document.Origin.ASSISTANT}


class DraftListInput(Input):
    limit: Limit = 20


@tool(name="draft_list", title="Entwürfe auflisten", scope="drafts:read", input_model=DraftListInput)
def draft_list(ctx, args: DraftListInput) -> Out:
    """Offene Entwürfe (Angebote, Rechnungen …), neueste zuerst, mit Kunde, Datum und ob eine Prüfung aussteht.
    Einzelheiten liefert draft_validate bzw. document_preview_link."""
    drafts = Document.objects.filter(company=ctx.company, status=Document.Status.DRAFT).select_related("customer")
    return Out({
        "drafts": [
            {
                "document_id": d.pk, "type": d.doc_type, "customer": customer_view(d.customer, ctx),
                "issue_date": d.issue_date.isoformat() if d.issue_date else None, "origin": d.origin,
                "needs_human_review": bool(d.ai_review_required and not d.ai_reviewed_at), "lines": d.lines.count(),
            }
            for d in drafts.order_by("-pk")[: args.limit]
        ]
    })  # fmt: skip


class DiscardInput(Input):
    document_id: Annotated[int, Field(gt=0)]


@tool(
    name="draft_discard",
    title="KI-Entwurf verwerfen",
    scope="drafts:write",
    input_model=DiscardInput,
    read_only=False,
    idempotent=False,
)
def draft_discard(ctx, args: DiscardInput) -> Out:
    """Löscht einen Entwurf, den ein KI-Client angelegt hat und den noch kein Mensch geprüft hat. Entwürfe aus der
    Oberfläche und festgeschriebene Dokumente bleiben unberührt."""
    doc = Document.objects.filter(company=ctx.company, pk=args.document_id).first()
    if doc is None or doc.status != Document.Status.DRAFT:
        raise ToolError("Nur Entwürfe lassen sich verwerfen")
    if doc.origin not in AI_ORIGINS or doc.ai_reviewed_at is not None:
        raise ToolError("Nur ungeprüfte Entwürfe eines KI-Clients lassen sich verwerfen – den Rest in der Oberfläche")
    record("document.discard", company=ctx.company, entity="document", entity_id=doc.pk,
           data={"type": doc.doc_type})  # fmt: skip
    doc.delete()
    return Out({"discarded": args.document_id})


class BankInput(Input):
    limit: Limit = 20


@tool(name="bank_open", title="Offene Bankumsätze", scope="reports:read", input_model=BankInput)
def bank_open(ctx, args: BankInput) -> Out:
    """Bankumsätze ohne Zuordnung mit Datum, Betrag, Verwendungszweck und passenden offenen Rechnungen.
    Zuordnen und Buchen geschieht in der Oberfläche; IBANs werden nie herausgegeben."""
    rows = BankTransaction.objects.filter(
        company=ctx.company, ignored=False, payment__isnull=True, expense__isnull=True
    ).order_by("-booked_on", "-pk")[: args.limit]
    return Out({
        "transactions": [
            {
                "booked_on": tx.booked_on.isoformat(), "amount": number(tx.amount), "purpose": tx.purpose[:140],
                "counterparty": "" if ctx.pseudonymise_persons else tx.counterparty,
                "suggestions": [
                    {"invoice": s.target.number, "amount": number(s.amount), "reason": s.reason}
                    for s in suggestions(tx) if isinstance(s.target, Document)
                ],
            }
            for tx in rows
        ]
    })  # fmt: skip
