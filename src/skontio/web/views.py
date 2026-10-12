from __future__ import annotations

from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.catalog.models import Item
from skontio.catalog.services import archive_item, search_items
from skontio.crm.models import Customer
from skontio.crm.services import create_customer, search_customers
from skontio.documents.models import INVOICE_TYPES, Document
from skontio.documents.renditions import de_amount
from skontio.payments.services import open_amount
from skontio.tax.engine import Regime
from skontio.taxstatus.services import TaxStatusError, regime_for, start_regime
from skontio.taxstatus.thresholds import small_business_status
from skontio.web.forms import CompanyForm, CustomerForm, ItemForm, SetupForm
from skontio.web.support import admin_required, company_view, current_company, render_page_or_fragment


@company_view
def dashboard(request):
    company, today = request.company, timezone.localdate()
    try:
        regime = regime_for(company, today)
    except TaxStatusError:
        regime = None
    status = small_business_status(company, today) if regime is Regime.SMALL_BUSINESS else None
    invoices = Document.objects.filter(
        company=company, doc_type__in=INVOICE_TYPES, status=Document.Status.FINALIZED
    ).exclude(children__doc_type=Document.Type.CANCELLATION)
    open_items = [(doc, open_amount(doc)) for doc in invoices.order_by("due_date")]
    open_items = [(doc, amount) for doc, amount in open_items if amount > 0]
    return render(
        request,
        "web/dashboard.html",
        {
            "regime": regime,
            "status": status,
            "revenue": de_amount(status.revenue) if status else "",
            "limit": de_amount(status.current_limit, 0) if status else "",
            "open_items": [(doc, de_amount(amount)) for doc, amount in open_items[:10]],
            "open_total": de_amount(sum((amount for _, amount in open_items), start=0)),
            "overdue": [doc for doc, _ in open_items if doc.due_date and doc.due_date < today],
            "drafts": Document.objects.filter(company=company, status=Document.Status.DRAFT).count(),
            "today": today,
        },
    )


@admin_required
def setup(request):
    company = current_company()
    form_class = CompanyForm if company else SetupForm
    form = form_class(request.POST or None, instance=company)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save()
            if company is None:
                start_regime(
                    saved,
                    Regime(form.cleaned_data["regime"]),
                    form.cleaned_data["regime_since"],
                    confirmed_by=request.user,
                )
        return redirect("/")
    return render(request, "web/setup.html", {"form": form, "is_new": company is None})


# --- customers --------------------------------------------------------------------------------


@company_view
def customer_list(request):
    q = request.GET.get("q", "")
    customers = search_customers(request.company, q)[:100]
    return render_page_or_fragment(
        request, "web/customers/list.html", "web/customers/_rows.html", {"customers": customers, "q": q}
    )


@company_view
def customer_create(request):
    form = CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        customer = create_customer(request.company, name=data.pop("name"), vat_id=data.pop("vat_id"), **data)
        return redirect(f"/kunden/{customer.pk}/")
    return render(request, "web/customers/form.html", {"form": form, "customer": None})


@company_view
def customer_detail(request, pk: int):
    customer = get_object_or_404(Customer, pk=pk, company=request.company)
    form = CustomerForm(request.POST or None, instance=customer)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(f"/kunden/{customer.pk}/")
    documents = Document.objects.filter(company=request.company, customer=customer)[:50]
    return render(request, "web/customers/form.html", {"form": form, "customer": customer, "documents": documents})


# --- catalog ----------------------------------------------------------------------------------


@company_view
def item_list(request):
    q = request.GET.get("q", "")
    items = search_items(request.company, q)[:200]
    return render_page_or_fragment(request, "web/items/list.html", "web/items/_rows.html", {"items": items, "q": q})


@company_view
def item_create(request):
    form = ItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.company = request.company
        item.save()
        return redirect("/leistungen/")
    return render(request, "web/items/form.html", {"form": form, "item": None})


@company_view
def item_edit(request, pk: int):
    item = get_object_or_404(Item, pk=pk, company=request.company)
    form = ItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("/leistungen/")
    return render(request, "web/items/form.html", {"form": form, "item": item})


@require_POST
@company_view
def item_archive(request, pk: int):
    archive_item(get_object_or_404(Item, pk=pk, company=request.company))
    return redirect("/leistungen/")
