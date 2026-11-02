"""Schnellsuche: one search box for contacts, documents, expenses and the catalogue."""

from __future__ import annotations

from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from skontio.catalog.models import Item
from skontio.crm.models import Customer
from skontio.documents.models import Document
from skontio.expenses.models import Expense
from skontio.web.document_views import document_state
from skontio.web.support import company_view

MAX_QUERY = 100  # longer input is cut, never searched as-is
PER_GROUP = 20
GROUPS = ("customers", "documents", "expenses", "items")


def search(company, query: str) -> dict:
    """Matches per group, newest/most relevant first; fewer than two characters find nothing."""
    q = " ".join(query.split())[:MAX_QUERY]
    result = {"q": q, **{group: [] for group in GROUPS}}
    if len(q) < 2:
        return result
    customers = Customer.objects.filter(company=company).filter(
        Q(name__icontains=q)
        | Q(number__icontains=q)
        | Q(email__icontains=q)
        | Q(city__icontains=q)
        | Q(vat_id__icontains=q)
        | Q(contacts__name__icontains=q)
    )
    documents = (
        Document.objects.filter(company=company)
        .filter(
            Q(number__icontains=q)
            | Q(customer__name__icontains=q)
            | Q(lines__text__icontains=q)
            | Q(lines__description__icontains=q)
        )
        .select_related("customer")
        .prefetch_related("payments", "children", "deductions")
    )
    expenses = Expense.objects.filter(company=company).filter(
        Q(number__icontains=q) | Q(supplier__icontains=q) | Q(description__icontains=q) | Q(note__icontains=q)
    )
    items = Item.objects.filter(company=company).filter(
        Q(name__icontains=q) | Q(sku__icontains=q) | Q(description__icontains=q)
    )
    today = timezone.localdate()
    result["customers"] = list(customers.distinct().order_by("archived_at", "name")[:PER_GROUP])
    result["documents"] = [
        (doc, document_state(doc, today)) for doc in documents.distinct().order_by("-issue_date", "-pk")[:PER_GROUP]
    ]
    result["expenses"] = list(expenses.order_by("-receipt_date", "-pk")[:PER_GROUP])
    result["items"] = list(items.order_by("archived_at", "name")[:PER_GROUP])
    return result


@company_view
def quick_search(request):
    result = search(request.company, request.GET.get("q", ""))
    result["total"] = sum(len(result[group]) for group in GROUPS)
    return render(request, "web/search.html", result)
