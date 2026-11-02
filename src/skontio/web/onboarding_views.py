"""Einrichtungsassistent (web): one page per step. Views only read forms, call services and render –
which step is done and what comes next is decided in ``core.onboarding``."""

from __future__ import annotations

from django.contrib import messages
from django.core.mail import EmailMessage
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from skontio.audit.services import record
from skontio.banking import psd2
from skontio.core import onboarding
from skontio.documents.layouts import TemplateError, default_template, save_template
from skontio.web.forms import AppearanceStepForm, BankStepForm, ShippingStepForm
from skontio.web.support import admin_required, current_company

LOGO_MAX_UPLOAD = 2 * 1024 * 1024


def _render(request, company, key: str, template: str, context: dict):
    return render(request, template, {"steps": onboarding.steps(company), "current": key, "wizard": True, **context})


def _continue(company, key: str):
    following = onboarding.next_step(company, after=key)
    if following is None:
        return redirect("/einrichtung/assistent/")
    return redirect(following.url + ("?assistent=1" if following.key == "unternehmen" else ""))


@admin_required
def overview(request):
    company = current_company()
    if company is None:
        return redirect("/einrichtung/")
    done, total = onboarding.progress(company)
    following = onboarding.next_step(company)
    return _render(request, company, "", "web/onboarding/overview.html",
                   {"done": done, "total": total, "following": following})  # fmt: skip


@admin_required
def appearance(request):
    company = current_company()
    if company is None:
        return redirect("/einrichtung/")
    current = default_template(company)
    initial = {"accent_color": current.accent_color, "layout": current.layout} if current else None
    form = AppearanceStepForm(request.POST or None, request.FILES or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["logo"]
        try:
            if upload is not None and upload.size > LOGO_MAX_UPLOAD:
                raise TemplateError("Das Logo darf höchstens 1 MB groß sein.")
            save_template(
                company, template=current, name=current.name if current else "Standard",
                layout=form.cleaned_data["layout"], accent_color=form.cleaned_data["accent_color"],
                footer_text=current.footer_text if current else "", is_default=True,
                logo=upload.read() if upload is not None else None,
            )  # fmt: skip
        except TemplateError as exc:
            form.add_error(None, str(exc))
        else:
            onboarding.mark(company, "aussehen")
            return _continue(company, "aussehen")
    return _render(request, company, "aussehen", "web/onboarding/appearance.html",
                   {"form": form, "template": current})  # fmt: skip


@admin_required
def bank(request):
    company = current_company()
    if company is None:
        return redirect("/einrichtung/")
    form = BankStepForm(request.POST or None, instance=company)
    if request.method == "POST" and form.is_valid():
        form.save()
        record("company.bank_step", actor=request.user, company=company,
               data={"auto_match": company.auto_match_payments})  # fmt: skip
        onboarding.mark(company, "bank")
        return _continue(company, "bank")
    connections = company.bank_connections.filter(status="active")
    return _render(request, company, "bank", "web/onboarding/bank.html",
                   {"form": form, "psd2": psd2.configured(), "connections": connections})  # fmt: skip


@admin_required
def numbers(request):
    company = current_company()
    if company is None:
        return redirect("/einrichtung/")
    if request.method == "POST":
        onboarding.mark(company, "nummern")
        return _continue(company, "nummern")
    previews = onboarding.number_previews(company, timezone.localdate())
    return _render(request, company, "nummern", "web/onboarding/numbers.html", {"previews": previews})


@admin_required
def shipping(request):
    company = current_company()
    if company is None:
        return redirect("/einrichtung/")
    form = ShippingStepForm(request.POST or None, instance=company)
    if request.method == "POST" and request.POST.get("aktion") == "testmail":
        to = request.user.email or company.email
        if not to:
            messages.error(request, "Für die Testmail fehlt eine E-Mail-Adresse (in deinem Konto oder der Firma).")
        else:
            try:
                EmailMessage(
                    "Skontio: Testmail", "Der E-Mail-Versand aus Skontio funktioniert.", to=[to],
                    reply_to=[company.email] if company.email else None,
                ).send()  # fmt: skip
            except Exception as exc:  # noqa: BLE001 – SMTP errors are shown, not raised
                messages.error(request, f"Testmail fehlgeschlagen: {type(exc).__name__}: {exc}"[:300])
            else:
                messages.success(request, f"Testmail an {to} verschickt – schau in dein Postfach.")
        return redirect("/einrichtung/assistent/versand/")
    if request.method == "POST" and form.is_valid():
        form.save()
        onboarding.mark(company, "versand")
        return _continue(company, "versand")
    return _render(request, company, "versand", "web/onboarding/shipping.html",
                   {"form": form, "smtp": onboarding.smtp_configured()})  # fmt: skip


@require_POST
@admin_required
def skip(request, key: str):
    company = current_company()
    if company is None:
        return redirect("/einrichtung/")
    try:
        onboarding.mark(company, key, onboarding.SKIPPED)
    except ValueError as exc:
        raise Http404 from exc
    return _continue(company, key)
