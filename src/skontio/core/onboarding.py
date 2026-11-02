"""Einrichtungsassistent: which setup steps are done, what comes next.

The state lives here, not in templates – a different UI (or the REST API later) uses the same
functions. A step is done when its data is there (e.g. a logo) or when the person confirmed or
skipped it; ``Company.onboarding`` stores those explicit marks.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.conf import settings

from skontio.core.models import Company
from skontio.numbering.models import NumberSequence
from skontio.numbering.services import NumberingError, format_number

DONE, SKIPPED = "done", "skipped"
# Pflichtangaben auf Geschäftsbriefen: Register (§ 37a, § 125a HGB, § 7 PartGG), Geschäftsführung (§ 35a GmbHG)
REGISTERED_FORMS = {
    Company.LegalForm.EK, Company.LegalForm.PARTG, Company.LegalForm.OHG, Company.LegalForm.KG,
    Company.LegalForm.UG, Company.LegalForm.GMBH,
}  # fmt: skip
DIRECTOR_FORMS = {Company.LegalForm.UG, Company.LegalForm.GMBH}


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    summary: str
    url: str
    state: str  # "done" | "skipped" | "open"
    optional: bool = False

    @property
    def finished(self) -> bool:
        return self.state in (DONE, SKIPPED)


def legal_problems(legal_form: str, *, register_court: str, register_number: str, managing_directors: str) -> dict:
    """Missing mandatory details for the legal form, as {field: message}."""
    problems = {}
    if legal_form in REGISTERED_FORMS:
        if not register_court.strip():
            problems["register_court"] = "Für diese Rechtsform gehört das Registergericht auf jede Rechnung."
        if not register_number.strip():
            problems["register_number"] = "Für diese Rechtsform gehört die Registernummer auf jede Rechnung."
    if legal_form in DIRECTOR_FORMS and not managing_directors.strip():
        problems["managing_directors"] = "Bei GmbH und UG gehören alle Geschäftsführer auf jede Rechnung."
    return problems


def _company_done(company: Company) -> bool:
    if not (company.legal_form and company.street and company.city and (company.tax_number or company.vat_id)):
        return False
    return not legal_problems(
        company.legal_form, register_court=company.register_court, register_number=company.register_number,
        managing_directors=company.managing_directors,
    )  # fmt: skip


def _has_logo(company: Company) -> bool:
    from skontio.documents.layouts import default_template

    template = default_template(company)
    return bool(template and template.logo_id)


def smtp_configured() -> bool:
    options = settings.MAILERS.get("default", {}).get("OPTIONS", {})
    return bool(options.get("host"))


def steps(company: Company) -> list[Step]:
    marks = company.onboarding or {}

    def state(key: str, automatic: bool) -> str:
        if automatic or marks.get(key) == DONE:
            return DONE
        return SKIPPED if marks.get(key) == SKIPPED else "open"

    return [
        Step("unternehmen", "Unternehmen", "Name, Rechtsform, Anschrift, Steuernummer", "/einrichtung/",
             state("unternehmen", _company_done(company))),
        Step("aussehen", "Logo und Farbe", "So sehen deine Rechnungen aus", "/einrichtung/assistent/aussehen/",
             state("aussehen", _has_logo(company)), optional=True),
        Step("bank", "Bankverbindung", "IBAN für Rechnungen, optional automatischer Kontoabruf",
             "/einrichtung/assistent/bank/", state("bank", bool(company.iban) and marks.get("bank") == DONE)),
        Step("nummern", "Nummern", "Wie deine Rechnungs- und Kundennummern aussehen",
             "/einrichtung/assistent/nummern/", state("nummern", False)),
        Step("versand", "Versand und Zahlung", "E-Mail-Versand, Zahlungsziel, Zahlungslink",
             "/einrichtung/assistent/versand/", state("versand", False)),
    ]  # fmt: skip


def progress(company: Company) -> tuple[int, int]:
    all_steps = steps(company)
    return sum(step.finished for step in all_steps), len(all_steps)


def complete(company: Company) -> bool:
    done, total = progress(company)
    return done == total


def next_step(company: Company, after: str | None = None) -> Step | None:
    """The first open step after ``after`` (or from the start); None when everything is finished."""
    all_steps = steps(company)
    keys = [step.key for step in all_steps]
    start = keys.index(after) + 1 if after in keys else 0
    return next((step for step in all_steps[start:] if not step.finished), None) or next(
        (step for step in all_steps if not step.finished), None
    )


def mark(company: Company, key: str, state: str = DONE) -> None:
    if key not in {step.key for step in steps(company)} or state not in (DONE, SKIPPED):
        raise ValueError(f"Unbekannter Schritt {key!r}")
    company.onboarding = {**(company.onboarding or {}), key: state}
    company.save(update_fields=["onboarding"])


def number_previews(company: Company, today: dt.date) -> list[tuple[str, str]]:
    """(label, next number) for the numbers a new user sees first."""
    from skontio.crm.services import CUSTOMER_SEQUENCE, DEFAULT_CUSTOMER_PATTERN
    from skontio.documents.models import Document
    from skontio.documents.services import DEFAULT_PATTERNS

    kinds = [
        ("Rechnungen", Document.Type.INVOICE, DEFAULT_PATTERNS[Document.Type.INVOICE]),
        ("Angebote", Document.Type.QUOTE, DEFAULT_PATTERNS[Document.Type.QUOTE]),
        ("Kunden", CUSTOMER_SEQUENCE, DEFAULT_CUSTOMER_PATTERN),
    ]
    result = []
    for label, key, default in kinds:
        seq = NumberSequence.objects.filter(company=company, doc_type=key).first()
        pattern, value = (seq.pattern, seq.next_value) if seq else (default, 1)
        if seq and seq.reset == NumberSequence.Reset.YEARLY and seq.period_year and seq.period_year < today.year:
            value = 1
        try:
            result.append((label, format_number(pattern, value, today)))
        except NumberingError:
            result.append((label, "–"))
    return result
