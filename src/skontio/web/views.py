from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from skontio.banking import psd2
from skontio.catalog.models import Item
from skontio.catalog.services import archive_item, search_items
from skontio.core import onboarding, watch
from skontio.crm.activities import ActivityError, complete_follow_up, create_follow_up, due_follow_ups, log_activity
from skontio.crm.models import Customer, FollowUp
from skontio.crm.services import CUSTOMER_ROLES, SUPPLIER_ROLES, create_customer, search_customers
from skontio.documents.models import INVOICE_TYPES, Document
from skontio.documents.renditions import de_amount
from skontio.dunning.services import candidates as dunning_candidates
from skontio.payments.services import open_amount
from skontio.recurring.services import due as recurring_due
from skontio.tax.engine import Regime
from skontio.taxstatus.services import TaxStatusError, regime_for, start_regime
from skontio.taxstatus.thresholds import small_business_status
from skontio.web.document_views import document_state
from skontio.web.forms import ActivityForm, CompanyForm, CustomerForm, FollowUpForm, ItemForm, SetupForm
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
    drafts = Document.objects.filter(company=company, status=Document.Status.DRAFT).select_related("customer")
    return render(
        request,
        "web/dashboard.html",
        {
            "regime": regime,
            "status": status,
            "revenue": de_amount(status.revenue) if status else "",
            "limit": de_amount(status.current_limit, 0) if status else "",
            "ruler": _threshold_ruler(status) if status else None,
            "open_items": [(doc, de_amount(amount)) for doc, amount in open_items[:10]],
            "open_total": de_amount(sum((amount for _, amount in open_items), start=0)),
            "overdue": [
                (doc, de_amount(amount)) for doc, amount in open_items if doc.due_date and doc.due_date < today
            ],
            "follow_ups": due_follow_ups(company, today),
            "dunning_due": len(dunning_candidates(company, today)),
            "recurring_due": len(recurring_due(company, today)),
            "drafts": drafts.count(),
            "draft_list": drafts.order_by("-pk")[:5],
            "today": today,
            "setup_progress": onboarding.progress(company) if request.user.is_staff else None,
            "setup_next": onboarding.next_step(company) if request.user.is_staff else None,
            "bank_expiring": psd2.expiring(company),
        },
    )


def _bar(value, limit) -> str:
    """Width of a ruler bar in percent (SVG attribute, the CSP forbids inline styles); at least a sliver."""
    if not limit:
        return "0"
    percent = min(float(value) / float(limit) * 100, 100.0)
    return f"{max(percent, 0.8 if value > 0 else 0):.1f}"


def _threshold_ruler(status) -> dict:
    """Both § 19 UStG limits: previous year (25.000 €) and current year (100.000 €)."""
    return {
        "current": _bar(status.revenue, status.current_limit),
        "previous": _bar(status.previous_year_revenue, status.previous_year_limit),
        "previous_revenue": de_amount(status.previous_year_revenue),
        "previous_limit": de_amount(status.previous_year_limit, 0),
    }


@admin_required
def setup(request):
    company = current_company()
    form_class = CompanyForm if company else SetupForm
    form = form_class(request.POST or None, instance=company)
    before = watch.snapshot(company) if company else None
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            saved = form.save()
            if before is not None:
                watch.report_changes(saved, before, user=request.user)
            if company is None:
                start_regime(
                    saved,
                    Regime(form.cleaned_data["regime"]),
                    form.cleaned_data["regime_since"],
                    confirmed_by=request.user,
                )
        if company is None or request.GET.get("assistent"):
            following = onboarding.next_step(saved, after="unternehmen")
            return redirect(following.url if following else "/einrichtung/assistent/")
        return redirect("/")
    wizard = company is None or bool(request.GET.get("assistent"))
    context = {"form": form, "is_new": company is None, "wizard": wizard,
               "steps": onboarding.steps(company) if company else [], "current": "unternehmen"}  # fmt: skip
    return render(request, "web/setup.html", context)


# --- customers --------------------------------------------------------------------------------


ROLE_FILTERS = {"kunden": CUSTOMER_ROLES, "lieferanten": SUPPLIER_ROLES}


@company_view
def customer_list(request):
    q = request.GET.get("q", "")
    role = request.GET.get("rolle", "")
    customers = search_customers(request.company, q, roles=ROLE_FILTERS.get(role))[:100]
    return render_page_or_fragment(
        request, "web/customers/list.html", "web/customers/_rows.html",
        {"customers": customers, "q": q, "role": role if role in ROLE_FILTERS else ""},
    )  # fmt: skip


@company_view
def customer_create(request):
    initial = {"role": "supplier"} if request.GET.get("rolle") == "lieferant" else None
    form = CustomerForm(request.POST or None, initial=initial)
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
    documents = Document.objects.filter(company=request.company, customer=customer).prefetch_related(
        "payments", "children", "deductions"
    )[:50]
    today = timezone.localdate()
    context = {
        "form": form,
        "customer": customer,
        "documents": [(doc, document_state(doc, today)) for doc in documents],
        "activities": customer.activities.select_related("document", "created_by")[:50],
        "follow_ups": customer.follow_ups.filter(done_at__isnull=True).select_related("document"),
        "activity_form": ActivityForm(),
        "follow_up_form": FollowUpForm(initial={"due_on": timezone.localdate() + dt.timedelta(days=7)}),
        "today": today,
        "attachments": customer.attachments.select_related("blob"),
        "expenses": customer.expenses.filter(cancelled_at__isnull=True)[:50],
        "attach_customer": customer,
    }
    return render(request, "web/customers/form.html", context)


@require_POST
@company_view
def activity_add(request, pk: int):
    customer = get_object_or_404(Customer, pk=pk, company=request.company)
    form = ActivityForm(request.POST)
    if form.is_valid():
        log_activity(customer, text=form.cleaned_data["text"], kind=form.cleaned_data["kind"], user=request.user)
    return redirect(f"/kunden/{customer.pk}/#aktivitaeten")


@require_POST
@company_view
def follow_up_add(request, pk: int):
    customer = get_object_or_404(Customer, pk=pk, company=request.company)
    form = FollowUpForm(request.POST)
    document = None
    if doc_id := request.POST.get("document", ""):
        document = get_object_or_404(Document, pk=doc_id, company=request.company)
    if form.is_valid():
        try:
            create_follow_up(customer, document=document, user=request.user, **form.cleaned_data)
        except ActivityError as exc:
            return HttpResponse(str(exc), status=409)
    return redirect(_back(request, f"/kunden/{customer.pk}/"))


@require_POST
@company_view
def follow_up_done(request, pk: int):
    follow_up = get_object_or_404(FollowUp, pk=pk, customer__company=request.company)
    complete_follow_up(follow_up, user=request.user, note=request.POST.get("note", ""))
    return redirect(_back(request, "/"))


def _back(request, default: str) -> str:
    """Return target from the form, only if it stays on this site (no open redirect)."""
    back = request.POST.get("back", "")
    return back if url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}) else default


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
