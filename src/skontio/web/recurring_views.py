"""Wiederkehrende Rechnungen: list, create from an invoice, pause/resume, generate due drafts."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.audit.services import record
from skontio.catalog.models import Unit
from skontio.documents.models import Document
from skontio.recurring.models import RecurringInvoice, RecurringLine
from skontio.recurring.services import RecurringError, create_from_invoice, generate, generate_due, next_issue_date
from skontio.web.forms import TAX_TREATMENT_LABELS
from skontio.web.support import company_view, full_access

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class RecurringForm(forms.Form):
    name = forms.CharField(label="Bezeichnung", max_length=120)
    interval = forms.TypedChoiceField(
        label="Rhythmus",
        choices=RecurringInvoice.Interval.choices,
        coerce=int,
        initial=RecurringInvoice.Interval.MONTHLY,
    )
    start_date = forms.DateField(label="Erster Leistungszeitraum ab", widget=DATE)
    in_advance = forms.BooleanField(
        label="Im Voraus abrechnen (sonst nach Ablauf des Zeitraums)", required=False, initial=True
    )
    auto_send = forms.BooleanField(
        label="Automatisch festschreiben und per E-Mail an den Kunden senden (ohne weitere Prüfung)", required=False
    )
    end_date = forms.DateField(label="Endet am (optional)", required=False, widget=DATE)


@company_view
def recurring_list(request):
    today = timezone.localdate()
    rows = [
        {
            "recurring": r,
            "next": next_issue_date(r),
            "due": (n := next_issue_date(r)) is not None and n <= today,
            "failed": list(
                r.runs.filter(send_failed_at__isnull=False).select_related("document").order_by("-period_start")[:3]
            ),
        }
        for r in RecurringInvoice.objects.filter(company=request.company).select_related("customer")
    ]
    return render(request, "web/recurring/list.html", {"rows": rows, "due_count": sum(r["due"] for r in rows)})


@require_POST
@company_view
@full_access
def recurring_from_invoice(request, pk: int):
    invoice = get_object_or_404(Document, pk=pk, company=request.company)
    form = RecurringForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Bitte Bezeichnung, Rhythmus und Beginn angeben.")
        return redirect(f"/dokumente/{invoice.pk}/")
    try:
        recurring = create_from_invoice(invoice, user=request.user, **form.cleaned_data)
    except RecurringError as exc:
        messages.error(request, str(exc))
        return redirect(f"/dokumente/{invoice.pk}/")
    messages.success(request, f"„{recurring.name}“ angelegt. Fällige Rechnungen entstehen als Entwürfe.")
    return redirect("recurring_list")


@require_POST
@company_view
def recurring_generate(request):
    drafts = generate_due(request.company, today=timezone.localdate(), user=request.user)
    if drafts:
        messages.success(request, f"{len(drafts)} Entwurf/Entwürfe angelegt – bitte prüfen und festschreiben.")
    else:
        messages.info(request, "Nichts fällig.")
    return redirect("/dokumente/?typ=invoice")


@require_POST
@company_view
@full_access
def recurring_toggle(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    recurring.active = not recurring.active
    recurring.save(update_fields=["active"])
    record(
        "recurring.resume" if recurring.active else "recurring.pause",
        actor=request.user,
        company=request.company,
        entity="recurring_invoice",
        entity_id=recurring.pk,
    )
    return redirect("recurring_list")


@require_POST
@company_view
@full_access
def recurring_toggle_send(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    if not recurring.auto_send and not recurring.customer.email:
        messages.error(request, "Für den automatischen Versand braucht der Kunde eine E-Mail-Adresse.")
        return redirect("recurring_list")
    recurring.auto_send = not recurring.auto_send
    recurring.save(update_fields=["auto_send"])
    record("recurring.auto_send_on" if recurring.auto_send else "recurring.auto_send_off", actor=request.user,
           company=request.company, entity="recurring_invoice", entity_id=recurring.pk)  # fmt: skip
    return redirect("recurring_list")


@require_POST
@company_view
def recurring_run(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    try:
        drafts = generate(recurring, today=timezone.localdate(), user=request.user)
    except RecurringError as exc:
        messages.error(request, str(exc))
        return redirect("recurring_list")
    if not drafts:
        messages.info(request, f"„{recurring.name}“ ist noch nicht fällig.")
        return redirect("recurring_list")
    return redirect(f"/dokumente/{drafts[-1].pk}/")


@require_POST
@company_view
@full_access
def recurring_forget_payer(request, pk: int):
    """Stop matching payments of this IBAN automatically (e.g. the customer changed banks)."""
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    old, recurring.payer_iban = recurring.payer_iban, ""
    recurring.save(update_fields=["payer_iban"])
    record("recurring.payer_forgotten", actor=request.user, company=request.company, entity="recurring_invoice",
           entity_id=recurring.pk, data={"iban": old})  # fmt: skip
    messages.success(request, f"Zahler für „{recurring.name}“ vergessen – die nächste Zahlung ordnest du selbst zu.")
    return redirect("recurring_list")


class RecurringEditForm(forms.ModelForm):
    class Meta:
        model = RecurringInvoice
        fields = ["name", "interval", "start_date", "end_date", "in_advance", "auto_send", "intro_text", "closing_text"]
        widgets = {
            "start_date": DATE,
            "end_date": DATE,
            "intro_text": forms.Textarea(attrs={"rows": 3}),
            "closing_text": forms.Textarea(attrs={"rows": 3}),
        }
        labels = {
            "in_advance": "Im Voraus abrechnen (sonst nach Ablauf des Zeitraums)",
            "auto_send": "Automatisch festschreiben und per E-Mail an den Kunden senden (ohne weitere Prüfung)",
            "end_date": "Endet am (optional)",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.generated:
            # the periods are counted from start_date in steps of the interval; changing either would shift
            # or duplicate periods that already have an invoice
            for name in ("interval", "start_date"):
                self.fields[name].disabled = True
                self.fields[name].help_text = "Nicht mehr änderbar, sobald Rechnungen entstanden sind."

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "Das Ende liegt vor dem Beginn.")
        if data.get("auto_send") and not self.instance.customer.email:
            self.add_error("auto_send", "Für den automatischen Versand braucht der Kunde eine E-Mail-Adresse.")
        return data


class RecurringLineForm(forms.ModelForm):
    unit_code = forms.ChoiceField(label="Einheit", choices=[])
    tax_treatment = forms.ChoiceField(label="Steuer", choices=list(TAX_TREATMENT_LABELS.items()))

    class Meta:
        model = RecurringLine
        fields = ["text", "description", "quantity", "unit_code", "unit_price", "discount_percent", "tax_treatment"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}
        labels = {"text": "Bezeichnung", "quantity": "Menge", "unit_price": "Einzelpreis netto",
                  "discount_percent": "Rabatt %", "description": "Beschreibung"}  # fmt: skip

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["unit_code"].choices = [(u.code, u.label_de) for u in Unit.objects.all()]
        self.fields["description"].required = False
        self.fields["discount_percent"].required = False

    def has_changed(self):
        # the blank extra row counts as untouched until someone types a text or a price
        if self.instance.pk is None:
            return bool(self.data.get(self.add_prefix("text")) or self.data.get(self.add_prefix("unit_price")))
        return super().has_changed()

    def clean_discount_percent(self):
        return self.cleaned_data.get("discount_percent") or 0


LineFormSet = forms.modelformset_factory(RecurringLine, form=RecurringLineForm, extra=1, can_delete=True)


@company_view
@full_access
def recurring_edit(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice.objects.select_related("customer"), pk=pk, company=request.company)
    lines = RecurringLine.objects.filter(recurring=recurring).order_by("position", "pk")
    extra = [{"quantity": 1, "unit_code": "C62", "tax_treatment": "standard"}]
    if request.method == "POST":
        form = RecurringEditForm(request.POST, instance=recurring)
        formset = LineFormSet(request.POST, queryset=lines, initial=extra)
        if form.is_valid() and formset.is_valid():
            kept = [f for f in formset.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")]
            if not kept:
                messages.error(request, "Mindestens eine Position wird gebraucht.")
            else:
                with transaction.atomic():
                    form.save()
                    for f in formset.deleted_forms:
                        if f.instance.pk:
                            f.instance.delete()
                    for position, f in enumerate(kept, start=1):
                        line = f.save(commit=False)
                        line.recurring, line.position = recurring, position
                        line.save()
                    record("recurring.update", actor=request.user, company=request.company,
                           entity="recurring_invoice", entity_id=recurring.pk)  # fmt: skip
                messages.success(request, f"„{recurring.name}“ gespeichert. Gilt ab der nächsten Rechnung.")
                return redirect("recurring_list")
    else:
        form = RecurringEditForm(instance=recurring)
        formset = LineFormSet(queryset=lines, initial=extra)
    return render(request, "web/recurring/edit.html", {"recurring": recurring, "form": form, "formset": formset})


@require_POST
@company_view
@full_access
def recurring_delete(request, pk: int):
    recurring = get_object_or_404(RecurringInvoice, pk=pk, company=request.company)
    if recurring.runs.filter(document__status=Document.Status.DRAFT).exists():
        messages.error(request, "Aus dieser Serie liegen noch Entwürfe vor – bitte erst festschreiben oder verwerfen.")
        return redirect("recurring_list")
    name = recurring.name
    pk_value = recurring.pk
    with transaction.atomic():
        # the invoices themselves stay untouched; only the link between series and invoice goes
        recurring.runs.all().delete()
        recurring.delete()
    record("recurring.delete", actor=request.user, company=request.company, entity="recurring_invoice",
           entity_id=pk_value, data={"name": name})  # fmt: skip
    messages.success(request, f"„{name}“ gelöscht.")
    return redirect("recurring_list")
