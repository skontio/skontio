"""Einstellungen → Vorlagen (letterhead) and Textbausteine."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from skontio.core.models import DOCUMENT_LANGUAGES
from skontio.documents.layouts import (
    LOGO_MAX_BYTES,
    TemplateError,
    delete_template,
    layout_context,
    save_template,
    text_block,
)
from skontio.documents.layouts import save_text_block as store_text_block
from skontio.documents.models import Document, DocumentTemplate
from skontio.web.support import admin_required, current_company


class TemplateForm(forms.Form):
    name = forms.CharField(label="Name", max_length=100)
    layout = forms.ChoiceField(label="Layout", choices=DocumentTemplate.Layout.choices)
    accent_color = forms.CharField(
        label="Akzentfarbe", max_length=7, widget=forms.TextInput(attrs={"type": "color"}), initial="#1f5f8b"
    )
    footer_text = forms.CharField(
        label="Zusatz in der Fußzeile",
        max_length=300,
        required=False,
        help_text="z. B. Inhaberin, Handelsregister, Website",
    )
    logo = forms.FileField(label="Logo (PNG oder JPEG, höchstens 1 MB)", required=False)
    remove_logo = forms.BooleanField(label="Logo entfernen", required=False)
    is_default = forms.BooleanField(label="Standardvorlage", required=False)


@admin_required
def template_list(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    templates = DocumentTemplate.objects.filter(company=company).select_related("logo")
    return render(request, "web/layouts/list.html", {"templates": templates, "form": TemplateForm()})


def _save(request, company, template: DocumentTemplate | None):
    form = TemplateForm(request.POST, request.FILES)
    if form.is_valid():
        upload = form.cleaned_data["logo"]
        if upload and upload.size > LOGO_MAX_BYTES:
            form.add_error("logo", "Das Logo darf höchstens 1 MB groß sein.")
        else:
            data = {k: v for k, v in form.cleaned_data.items() if k != "logo"}
            try:
                saved = save_template(company, template=template, logo=upload.read() if upload else None, **data)
            except TemplateError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Vorlage „{saved.name}“ gespeichert.")
                return redirect("template_list"), form
    return None, form


@admin_required
@require_POST
def template_create(request):
    company = current_company()
    response, form = _save(request, company, None)
    if response:
        return response
    templates = DocumentTemplate.objects.filter(company=company)
    return render(request, "web/layouts/list.html", {"templates": templates, "form": form}, status=400)


@admin_required
def template_edit(request, pk: int):
    company = current_company()
    template = get_object_or_404(DocumentTemplate, pk=pk, company=company)
    if request.method == "POST":
        response, form = _save(request, company, template)
        if response:
            return response
    else:
        form = TemplateForm(
            initial={
                "name": template.name,
                "layout": template.layout,
                "accent_color": template.accent_color,
                "footer_text": template.footer_text,
                "is_default": template.is_default,
            }
        )
    context = {"template": template, "form": form, "logo": layout_context(template)["logo"]}
    return render(request, "web/layouts/edit.html", context)


@admin_required
@require_POST
def template_delete(request, pk: int):
    template = get_object_or_404(DocumentTemplate, pk=pk, company=current_company())
    try:
        delete_template(template)
    except TemplateError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Vorlage „{template.name}“ gelöscht.")
    return redirect("template_list")


TEXT_BLOCK_TYPES = [t for t in Document.Type if t != Document.Type.CANCELLATION]


@admin_required
def text_blocks(request):
    company = current_company()
    if company is None:
        return redirect("setup")
    if request.method == "POST":
        for doc_type in TEXT_BLOCK_TYPES:
            for language, _ in DOCUMENT_LANGUAGES:
                key = f"{doc_type.value}-{language}"
                store_text_block(
                    company,
                    doc_type,
                    language,
                    intro_text=request.POST.get(f"{key}-intro", "")[:5000],
                    closing_text=request.POST.get(f"{key}-closing", "")[:5000],
                )
        messages.success(request, "Textbausteine gespeichert.")
        return redirect("text_blocks")
    rows = []
    for doc_type in TEXT_BLOCK_TYPES:
        for language, language_label in DOCUMENT_LANGUAGES:
            block = text_block(company, doc_type, language)
            rows.append(
                {
                    "key": f"{doc_type.value}-{language}",
                    "label": f"{doc_type.label} · {language_label}",
                    "intro": block.intro_text if block else "",
                    "closing": block.closing_text if block else "",
                }
            )
    return render(request, "web/layouts/text_blocks.html", {"rows": rows})
