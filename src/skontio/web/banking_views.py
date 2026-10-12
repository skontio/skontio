"""Bank: import statements (CAMT.053/CSV), match transactions to invoices and expenses."""

from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from skontio.banking.models import BankTransaction
from skontio.banking.services import BankingError, book_payment, ignore, import_statement, link_expense, suggestions
from skontio.documents.models import Document
from skontio.documents.renditions import de_amount
from skontio.expenses.models import Expense
from skontio.payments.models import PaymentError
from skontio.web.support import company_view


@company_view
def bank_list(request):
    show_all = request.GET.get("alle") == "1"
    transactions = BankTransaction.objects.filter(company=request.company).select_related(
        "payment__document", "expense"
    )
    if not show_all:
        transactions = transactions.filter(ignored=False, payment__isnull=True, expense__isnull=True)
    rows = [
        {
            "tx": tx,
            "amount": de_amount(tx.amount),
            "suggestions": [
                {
                    "kind": "invoice" if isinstance(s.target, Document) else "expense",
                    "target": s.target,
                    "amount": de_amount(s.amount),
                    "reason": s.reason,
                }
                for s in suggestions(tx)
            ],
        }
        for tx in transactions[:200]
    ]
    open_count = BankTransaction.objects.filter(
        company=request.company, ignored=False, payment__isnull=True, expense__isnull=True
    ).count()
    return render(request, "web/banking/list.html", {"rows": rows, "show_all": show_all, "open_count": open_count})


@require_POST
@company_view
def bank_import(request):
    upload = request.FILES.get("statement")
    if upload is None:
        messages.error(request, "Bitte eine Datei auswählen.")
        return redirect("bank_list")
    try:
        created, known = import_statement(request.company, upload.read(), user=request.user)
    except BankingError as exc:
        messages.error(request, f"Import nicht möglich: {exc}")
    else:
        messages.success(request, f"{created} neue Umsätze importiert, {known} waren schon bekannt.")
    return redirect("bank_list")


def _transaction(request, pk: int) -> BankTransaction:
    return get_object_or_404(BankTransaction, pk=pk, company=request.company)


@require_POST
@company_view
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
def bank_ignore(request, pk: int):
    ignore(_transaction(request, pk), user=request.user)
    return redirect("bank_list")
