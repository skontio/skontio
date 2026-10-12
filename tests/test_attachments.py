"""Dokumentenablage: Typprüfung am Inhalt, Archiv, Löschen nur ohne Aufbewahrungsfrist."""

from __future__ import annotations

import datetime as dt
import io
import zipfile

import pytest

from skontio.archive.models import Blob
from skontio.attachments.models import Attachment
from skontio.attachments.services import AttachmentError, add_attachment, delete_attachment, detect_mime, safe_filename
from skontio.documents.models import Document
from skontio.documents.services import create_draft

PDF = b"%PDF-1.7\n% Vertrag\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def office(member: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, "<x/>")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "name", "mime"),
    [
        (PDF, "vertrag.pdf", "application/pdf"),
        (PNG, "foto.png", "image/png"),
        (b"\xff\xd8\xff\xe0", "foto.jpg", "image/jpeg"),
        ("Notiz äöü".encode(), "notiz.txt", "text/plain"),
        (b"<?xml version='1.0'?><a/>", "x.xml", "application/xml"),
        (
            office("word/document.xml"),
            "brief.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    ],
)
def test_allowed_types_are_detected_by_content(data, name, mime):
    assert detect_mime(data, name) == mime


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"MZ\x90\x00binary", "rechnung.pdf"),  # Windows executable disguised as PDF
        (office("payload.exe"), "brief.docx"),  # zip that is not an office document
        (office("word/document.xml"), "archiv.zip"),
        (b"\x00\x01\x02", "daten.bin"),
    ],
)
def test_other_types_are_rejected(data, name):
    with pytest.raises(AttachmentError):
        detect_mime(data, name)


def test_filename_is_sanitised():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename('C:\\Users\\x\\a"<b>.pdf') == "a__b_.pdf"
    assert safe_filename("   ") == "datei"


def test_upload_limits(shop):
    with pytest.raises(AttachmentError, match="leer"):
        add_attachment(shop.company, b"", filename="a.pdf", customer=shop.org)
    with pytest.raises(AttachmentError, match="20 MB"):
        add_attachment(shop.company, PDF + b"x" * (20 * 1024 * 1024), filename="a.pdf", customer=shop.org)
    with pytest.raises(AttachmentError, match="Kunden oder ein Dokument"):
        add_attachment(shop.company, PDF, filename="a.pdf")


def test_delete_without_retention_removes_the_file(shop, settings, django_capture_on_commit_callbacks):
    attachment = add_attachment(shop.company, PDF, filename="a.pdf", customer=shop.org)
    path = settings.ARCHIVE_DIR / attachment.blob.storage_key
    assert path.exists()
    with django_capture_on_commit_callbacks(execute=True):
        assert delete_attachment(attachment) is True
    assert not path.exists() and not Blob.objects.exists()


def test_delete_with_retention_keeps_the_file(shop, settings):
    attachment = add_attachment(shop.company, PDF, filename="vertrag.pdf", customer=shop.org, keep=True)
    assert attachment.blob.retention_until == dt.date(dt.date.today().year + 6, 12, 31)
    assert delete_attachment(attachment) is False
    assert not Attachment.objects.exists()
    assert (settings.ARCHIVE_DIR / attachment.blob.storage_key).exists()


def test_shared_blob_is_kept_while_referenced(shop):
    first = add_attachment(shop.company, PDF, filename="a.pdf", customer=shop.org)
    add_attachment(shop.company, PDF, filename="kopie.pdf", customer=shop.person)
    assert delete_attachment(first) is False
    assert Blob.objects.count() == 1


def test_ui_upload_download_delete(client, shop):
    client.force_login(shop.user)
    document = create_draft(shop.company, Document.Type.QUOTE, shop.org, issue_date=dt.date(2026, 10, 12))
    client.post("/dateien/hochladen/", {"document": document.pk, "file": io.BytesIO(PDF), "note": "Auftrag"})
    attachment = Attachment.objects.get()
    assert attachment.document == document and attachment.customer == shop.org
    page = client.get(f"/dokumente/{document.pk}/").content.decode()
    assert page.count(f"/dateien/{attachment.pk}/") == 2  # download + delete, once each
    title = page.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "<" not in title  # the attachment list once leaked into the title block
    response = client.get(f"/dateien/{attachment.pk}/")
    assert b"".join(response.streaming_content) == PDF
    assert "sandbox" in response["Content-Security-Policy"]
    response = client.post("/dateien/hochladen/", {"customer": shop.org.pk, "file": io.BytesIO(b"MZ\x90")})
    assert "Dateityp nicht erlaubt" in client.get(response["Location"]).content.decode()
    client.post(f"/dateien/{attachment.pk}/loeschen/")
    assert not Attachment.objects.exists()


def test_other_companies_files_are_not_reachable(client, shop, django_user_model):
    from skontio.core.models import Company
    from skontio.crm.services import create_customer

    other = Company.objects.create(name="Fremd")
    attachment = add_attachment(other, PDF, filename="geheim.pdf", customer=create_customer(other, name="X"))
    client.force_login(shop.user)
    assert client.get(f"/dateien/{attachment.pk}/").status_code == 404
