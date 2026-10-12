"""Vorlagen (letterhead: layout, colour, logo, footer) and Textbausteine (default texts).

A finalized document records the template it was rendered with; the archived PDF never changes
when a template is edited later. Logos are PNG or JPEG, checked by content and decoded once with
Pillow so a broken file cannot reach the PDF renderer.
"""

from __future__ import annotations

import base64
import re
from io import BytesIO

from django.db import transaction
from PIL import Image, UnidentifiedImageError

from skontio.archive.services import load, release, store
from skontio.attachments.services import AttachmentError, detect_mime
from skontio.documents.models import Document, DocumentTemplate, TextBlock

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
LOGO_MAX_BYTES = 1_000_000
LOGO_MAX_PIXELS = 4000 * 4000
LOGO_TYPES = {"image/png", "image/jpeg"}
FALLBACK = {"layout": DocumentTemplate.Layout.CLASSIC, "accent_color": "#1f5f8b", "logo": "", "footer_text": ""}


class TemplateError(ValueError):
    pass


def _check_logo(data: bytes) -> str:
    if len(data) > LOGO_MAX_BYTES:
        raise TemplateError("Das Logo darf höchstens 1 MB groß sein.")
    try:
        mime = detect_mime(data, "logo")
    except AttachmentError as exc:
        raise TemplateError("Logo bitte als PNG oder JPEG hochladen.") from exc
    if mime not in LOGO_TYPES:
        raise TemplateError("Logo bitte als PNG oder JPEG hochladen.")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.width * image.height > LOGO_MAX_PIXELS:
                raise TemplateError("Das Logo ist zu groß (höchstens 4000 × 4000 Pixel).")
            image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise TemplateError("Die Logo-Datei ist beschädigt.") from exc
    return mime


def save_template(
    company,
    *,
    template: DocumentTemplate | None = None,
    name: str,
    layout: str = DocumentTemplate.Layout.CLASSIC,
    accent_color: str = FALLBACK["accent_color"],
    footer_text: str = "",
    is_default: bool = False,
    logo: bytes | None = None,
    remove_logo: bool = False,
) -> DocumentTemplate:
    name = name.strip()
    if not name:
        raise TemplateError("Bitte einen Namen angeben.")
    if layout not in DocumentTemplate.Layout.values:
        raise TemplateError("Unbekanntes Layout.")
    if not HEX_COLOR.match(accent_color or ""):
        raise TemplateError("Akzentfarbe bitte als #RRGGBB angeben.")
    mime = _check_logo(logo) if logo else None
    with transaction.atomic():
        template = template or DocumentTemplate(company=company)
        if template.company_id != company.pk:
            raise TemplateError("Die Vorlage gehört nicht zu dieser Firma.")
        old_logo = template.logo
        template.name, template.layout, template.accent_color = name[:100], layout, accent_color.lower()
        template.footer_text = footer_text.strip()[:300]
        if logo:
            template.logo = store(logo, mime=mime)
        elif remove_logo:
            template.logo = None
        others = DocumentTemplate.objects.filter(company=company).exclude(pk=template.pk)
        if is_default or not others.filter(is_default=True).exists():  # there is always exactly one default
            others.update(is_default=False)
            template.is_default = True
        template.save()
        if old_logo and old_logo != template.logo:
            transaction.on_commit(lambda: _release_quietly(old_logo))
    return template


def _release_quietly(blob) -> None:
    with transaction.atomic():
        release(blob)  # keeps it if another template still uses the same image


def delete_template(template: DocumentTemplate) -> None:
    if template.documents.exists():
        raise TemplateError("Mit dieser Vorlage wurden schon Dokumente erstellt – sie bleibt erhalten.")
    with transaction.atomic():
        logo, was_default, company = template.logo, template.is_default, template.company
        template.delete()
        if was_default:
            successor = DocumentTemplate.objects.filter(company=company).order_by("name").first()
            if successor:
                successor.is_default = True
                successor.save(update_fields=["is_default"])
        if logo:
            transaction.on_commit(lambda: _release_quietly(logo))


def default_template(company) -> DocumentTemplate | None:
    return DocumentTemplate.objects.filter(company=company, is_default=True).select_related("logo").first()


def template_for(document: Document) -> DocumentTemplate | None:
    return document.template or default_template(document.company)


def layout_context(template: DocumentTemplate | None) -> dict:
    if template is None:
        return dict(FALLBACK)
    logo = ""
    if template.logo:
        logo = f"data:{template.logo.mime};base64,{base64.b64encode(load(template.logo)).decode('ascii')}"
    return {
        "layout": template.layout,
        "accent_color": template.accent_color,
        "logo": logo,
        "footer_text": template.footer_text,
    }


def text_block(company, doc_type, language: str) -> TextBlock | None:
    return TextBlock.objects.filter(company=company, doc_type=doc_type, language=language).first()


def save_text_block(company, doc_type, language: str, *, intro_text: str, closing_text: str) -> None:
    intro_text, closing_text = intro_text.strip(), closing_text.strip()
    if not intro_text and not closing_text:
        TextBlock.objects.filter(company=company, doc_type=doc_type, language=language).delete()
        return
    TextBlock.objects.update_or_create(
        company=company,
        doc_type=doc_type,
        language=language,
        defaults={"intro_text": intro_text, "closing_text": closing_text},
    )
