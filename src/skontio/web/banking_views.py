"""Bank: import statements (CAMT.053/CSV), match transactions to invoices and expenses."""

from __future__ import annotations

import re

from django import forms
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from skontio.audit.services import record
from skontio.banking.models import BankAccount, BankRule, BankTransaction
from skontio.banking.parsers import StatementError, parse_amount
from skontio.banking.services import (
    BankingError,
    apply_ignore_rules,
    book_payment,
    create_rule,
    ignore,
    import_statement,
    link_expense,
    rule_for,
    suggestions,
)
from skontio.documents.models import Document
from skontio.documents.renditions import de_amount
from skontio.expenses.models import Expense
from skontio.payments.models import PaymentError
from skontio.web.document_views import _iso_date
from skontio.web.forms import normalise_iban
from skontio.web.support import company_view, full_access

CATEGORIES = dict(Expense.Category.choices)
STATES = {"offen": "offen", "erledigt": "zugeordnet oder ignoriert", "alle": "alle"}
MAX_QUERY = 100


def _account(request, key: str = "konto") -> BankAccount | None:
    raw = request.GET.get(key) or request.POST.get(key) or ""
    if not (raw.isascii() and raw.isdigit() and len(raw) < 19):
        return None
    return BankAccount.objects.filter(pk=raw, company=request.company).first()


def _filtered(request):
    transactions = BankTransaction.objects.filter(company=request.company)
    state = request.GET.get("status", "")
    if request.GET.get("alle") == "1":  # old links
        state = "alle"
    state = state if state in STATES else "offen"
    open_q = Q(ignored=False, payment__isnull=True, expense__isnull=True)
    if state == "offen":
        transactions = transactions.filter(open_q)
    elif state == "erledigt":
        transactions = transactions.exclude(open_q)
    account = _account(request)
    if account is not None:
        transactions = transactions.filter(account=account)
    query = " ".join(request.GET.get("q", "").split())[:MAX_QUERY]
    if query:
        match = (
            Q(counterparty__icontains=query) | Q(purpose__icontains=query) | Q(iban__icontains=query.replace(" ", ""))
        )
        try:
            amount = parse_amount(query)
        except StatementError:
            amount = None
        if amount is not None and amount and re.fullmatch(r"-?[\d.,]+", query.replace(" ", "")):
            match |= Q(amount=amount) | Q(amount=-amount)
        transactions = transactions.filter(match)
    start, end = _iso_date(request.GET.get("von", "")), _iso_date(request.GET.get("bis", ""))
    if start:
        transactions = transactions.filter(booked_on__gte=start)
    if end:
        transactions = transactions.filter(booked_on__lte=end)
    filters = {"status": state, "konto": account.pk if account else "", "q": query, "von": start, "bis": end}
    return transactions, filters


@company_view
@full_access
def bank_list(request):
    transactions, filters = _filtered(request)
    transactions = transactions.select_related("payment__document", "expense", "account")
    rules = list(BankRule.objects.filter(company=request.company))
    rows = [
        {
            "tx": tx,
            "amount": de_amount(tx.amount),
            "rule": (rule := rule_for(tx, rules)),
            "rule_category": CATEGORIES.get(rule.category, "") if rule else "",
            "suggestions": [
                {
                    "kind": "invoice" if isinstance(s.target, Document) else "expense",
                    "target": s.target,
                    "amount": de_amount(s.amount),
                    "reason": s.reason,
                }
                for s in suggestions(tx)
            ]
            if not tx.done
            else [],
        }
        for tx in transactions[:200]
    ]
    accounts = BankAccount.objects.filter(company=request.company)
    open_count = BankTransaction.objects.filter(
        company=request.company, ignored=False, payment__isnull=True, expense__isnull=True
    ).count()
    context = {
        "rows": rows,
        "open_count": open_count,
        "accounts": accounts,
        "states": STATES,
        "account_form": AccountForm(),
        "rules": [(r, CATEGORIES.get(r.category, "")) for r in rules],
        "rule_form": RuleForm(),
        **filters,
    }
    return render(request, "web/banking/list.html", context)


class AccountForm(forms.Form):
    name = forms.CharField(label="Bezeichnung", max_length=100)
    iban = forms.CharField(label="IBAN", max_length=42, required=False)

    def clean_iban(self):
        return normalise_iban(self.cleaned_data["iban"])


@require_POST
@company_view
@full_access
def bank_account_create(request):
    form = AccountForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Konto nicht angelegt: " + " ".join(e for errs in form.errors.values() for e in errs))
        return redirect("bank_list")
    iban = form.cleaned_data["iban"]
    if iban and BankAccount.objects.filter(company=request.company, iban=iban).exists():
        messages.error(request, "Ein Konto mit dieser IBAN gibt es schon.")
        return redirect("bank_list")
    account = BankAccount.objects.create(company=request.company, name=form.cleaned_data["name"].strip(), iban=iban)
    record("bank.account_create", actor=request.user, company=request.company, entity="bank_account",
           entity_id=account.pk, data={"name": account.name})  # fmt: skip
    messages.success(request, f"Konto „{account.name}“ angelegt.")
    return redirect("bank_list")


class RuleForm(forms.Form):
    counterparty_contains = forms.CharField(label="Gegenpartei enthält", max_length=100, required=False)
    purpose_contains = forms.CharField(label="Verwendungszweck enthält", max_length=100, required=False)
    direction = forms.ChoiceField(label="Richtung", choices=BankRule.Direction.choices)
    action = forms.ChoiceField(label="Dann", choices=BankRule.Action.choices)
    category = forms.ChoiceField(label="Kategorie", choices=[("", "–"), *Expense.Category.choices], required=False)
    supplier = forms.CharField(label="Lieferant (für die Ausgabe)", max_length=200, required=False)


@require_POST
@company_view
@full_access
def bank_rule_create(request):
    form = RuleForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Regel nicht angelegt: Eingaben prüfen.")
        return redirect("bank_list")
    try:
        rule = create_rule(request.company, user=request.user, **form.cleaned_data)
    except BankingError as exc:
        messages.error(request, f"Regel nicht angelegt: {exc}")
    else:
        messages.success(request, f"Regel angelegt: {rule}")
    return redirect("bank_list")


@require_POST
@company_view
@full_access
def bank_rule_delete(request, pk: int):
    rule = get_object_or_404(BankRule, pk=pk, company=request.company)
    record("bank.rule_delete", actor=request.user, company=request.company, entity="bank_rule", entity_id=rule.pk,
           data={"rule": str(rule)})  # fmt: skip
    rule.delete()
    return redirect("bank_list")


@require_POST
@company_view
@full_access
def bank_rules_apply(request):
    count = apply_ignore_rules(request.company, user=request.user)
    messages.success(request, f"{count} Umsätze nach Regeln als privat/ignoriert markiert.")
    return redirect("bank_list")


@require_POST
@company_view
@full_access
def bank_import(request):
    upload = request.FILES.get("statement")
    if upload is None:
        messages.error(request, "Bitte eine Datei auswählen.")
        return redirect("bank_list")
    account = _account(request)
    try:
        created, known = import_statement(request.company, upload.read(), user=request.user, account=account)
    except BankingError as exc:
        messages.error(request, f"Import nicht möglich: {exc}")
    else:
        messages.success(request, f"{created} neue Umsätze importiert, {known} waren schon bekannt.")
    return redirect("bank_list")


def _transaction(request, pk: int) -> BankTransaction:
    return get_object_or_404(BankTransaction, pk=pk, company=request.company)


@require_POST
@company_view
@full_access
def bank_match(request, pk: int):
    tx = _transaction(request, pk)
    try:
        if invoice_pk := request.POST.get("invoice"):
            invoice = get_object_or_404(Document, pk=invoice_pk, company=request.company)
            book_payment(tx, invoice, user=request.user)
            messages.success(request, f"Zahlung auf {invoice.number} gebucht.")
        elif expense_pk := request.POST.get("expense"):
            expense = get_object_or_404(Expense, pk=expense_pk, company=request.company)
            link_expense(tx, expense, user=request.user)
            messages.success(request, f"Ausgabe {expense.number} als bezahlt markiert.")
    except (BankingError, PaymentError) as exc:
        messages.error(request, str(exc))
    return redirect("bank_list")


@require_POST
@company_view
@full_access
def bank_ignore(request, pk: int):
    ignore(_transaction(request, pk), user=request.user)
    return redirect("bank_list")
