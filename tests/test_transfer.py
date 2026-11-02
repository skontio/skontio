"""CSV-Import und -Export für Kontakte und Leistungen: Rundreise, alles-oder-nichts, Müll und Angriffe."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from skontio.accounts.roles import DRAFTS_ONLY_GROUP
from skontio.audit.models import AuditEvent
from skontio.catalog.models import Item
from skontio.crm.models import Customer
from skontio.crm.services import create_customer
from skontio.web.transfer import FileProblem, import_customers, import_items, parse_amount, read_table

HEADER = "Rolle;Art;Name;USt-IdNr.;E-Mail;Straße;PLZ;Ort;Land;Zahlungsziel;Tags\n"
ITEM_HEADER = "Artikelnummer;Art;Bezeichnung;Einheit;Nettopreis;Steuer\n"


def csv(text: str, encoding="utf-8") -> bytes:
    return text.encode(encoding)


def upload(data: bytes):
    return SimpleUploadedFile("import.csv", data, content_type="text/csv")


# ---- contacts ----------------------------------------------------------------------------------


def test_contacts_are_created_with_numbers_roles_and_tags(shop):
    data = csv(
        HEADER
        + "Kunde;Firma;Neu GmbH;DE 123 456 789;neu@x.example;Weg 2;10115;Berlin;de;30;Stammkunde, VIP\n"
        + "Lieferant;Firma;Druckerei;;;;;;;;\n"
        + "Kunde und Lieferant;Privatperson;Max Muster;;;;;;AT;;\n"
    )
    result = import_customers(shop.company, data, user=shop.user)
    assert (result.created, result.errors) == (3, [])
    new = Customer.objects.get(name="Neu GmbH")
    assert (new.vat_id, new.country, new.payment_terms_days) == ("DE123456789", "DE", 30)
    assert sorted(t.name for t in new.tags.all()) == ["Stammkunde", "VIP"]
    assert Customer.objects.get(name="Druckerei").number.startswith("L-")
    assert Customer.objects.get(name="Max Muster").kind == Customer.Kind.PERSON
    assert AuditEvent.objects.filter(action="contacts.imported").exists()


def test_one_bad_row_imports_nothing_and_names_every_problem(shop):
    before = Customer.objects.count()
    data = csv(
        HEADER
        + "Kunde;Firma;Gut GmbH;;;;;;;;\n"
        + "Chef;Firma;Falsche Rolle;;;;;;;;\n"
        + "Kunde;Firma;;;;;;;;;\n"
        + "Kunde;Firma;Mail kaputt;;keine-mail;;;;;;\n"
        + "Kunde;Firma;USt kaputt;DE12;;;;;;;\n"
        + "Kunde;Firma;Land kaputt;;;;;;Deutschland;;\n"
        + "Kunde;Firma;Ziel kaputt;;;;;;;9999;\n"
    )
    result = import_customers(shop.company, data)
    assert result.created == 0 and Customer.objects.count() == before
    lines = " ".join(result.errors)
    for line in (3, 4, 5, 6, 7, 8):
        assert f"Zeile {line}:" in lines
    assert "Zeile 2:" not in lines


def test_existing_contacts_are_skipped_never_changed(shop):
    data = csv(HEADER + f"Kunde;Firma;{shop.org.name.upper()};;evil@x.example;;{shop.org.postcode};;;;\n")
    result = import_customers(shop.company, data)
    assert result.created == 0 and len(result.skipped) == 1
    shop.org.refresh_from_db()
    assert shop.org.email == "a@kunde.example"


def test_duplicates_inside_the_file_are_imported_once(shop):
    result = import_customers(shop.company, csv(HEADER + "Kunde;Firma;Doppelt;;;;1;;;;\n" * 3))
    assert result.created == 1 and len(result.skipped) == 2


def test_export_reads_back_unchanged(client, shop):
    create_customer(shop.company, name="=HYPERLINK(1)", street="+S", postcode="-1", city="@K", tags=["a"])
    client.force_login(shop.user)
    exported = client.get("/kunden/export.csv").content
    assert b"'=HYPERLINK" in exported and b";=" not in exported
    Customer.objects.filter(company=shop.company).delete()
    result = import_customers(shop.company, exported)
    assert result.errors == [] and result.created == 3
    evil = Customer.objects.get(name="=HYPERLINK(1)")
    assert (evil.street, evil.postcode, evil.city) == ("+S", "-1", "@K")  # plain text; the export escapes again


def test_windows_1252_and_comma_separated_files(shop):
    data = csv('Name,Ort\n"Müller, Söhne",Köln\n', encoding="cp1252")
    assert import_customers(shop.company, data).created == 1
    assert Customer.objects.get(name="Müller, Söhne").city == "Köln"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "leer"),
        (b"Vorname;Ort\nA;B\n", "Es fehlen Spalten: Name"),
        (b"Name\n", "keine Datenzeilen"),
        (b"Name\n\x00\x01\x02", "keine CSV"),
        (b"Name\n" + b"x\n" * 5001, "Höchstens 5000"),
        (b"Name\n" + b"x" * (2 * 1024 * 1024), "größer als 2 MB"),
    ],
)
def test_unusable_files_are_refused_with_a_reason(shop, data, message):
    with pytest.raises(FileProblem, match=message):
        import_customers(shop.company, data)


def test_overlong_values_are_errors_not_crashes(shop):
    result = import_customers(shop.company, csv(f"Name;Ort\n{'N' * 5000};{'O' * 5000}\n"))
    assert result.created == 0 and "Zeile 2" in result.errors[0]


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.binary(max_size=2000))
def test_random_bytes_only_raise_our_own_error(shop, data):
    try:
        import_customers(shop.company, b"Name;Rolle;Land;Zahlungsziel;Tags\n" + data)
    except FileProblem:
        pass


# ---- items -------------------------------------------------------------------------------------


def test_items_accept_codes_labels_and_german_amounts(shop):
    data = csv(
        ITEM_HEADER
        + "W-1;Leistung;Webdesign;Stunde;1.234,50;Regelsatz (19 %)\n"
        + "B-1;Artikel;Buch;C62;19.90;reduced\n"
        + ";;Pauschale;LS;80 €;\n"
    )
    result = import_items(shop.company, data, user=shop.user)
    assert (result.created, result.errors) == (3, [])
    assert Item.objects.get(sku="W-1").net_price == Decimal("1234.5")
    assert Item.objects.get(sku="B-1").tax_treatment == "reduced"
    assert Item.objects.get(name="Pauschale").unit.code == "LS"


@pytest.mark.parametrize("price", ["-5", "abc", "1e30", "NaN", "Infinity", "99999999999", "1,2,3", ""])
def test_bad_prices_import_nothing(shop, price):
    before = Item.objects.count()
    result = import_items(shop.company, csv(ITEM_HEADER + f"X;;Gut;LS;1;\nY;;Schlecht;LS;{price};\n"))
    assert Item.objects.count() == before and "Zeile 3" in result.errors[0]


def test_unknown_unit_and_tax_are_reported(shop):
    result = import_items(shop.company, csv(ITEM_HEADER + "X;;A;Kilogramm-Fass;1;\nY;;B;LS;1;steuerfrei-ish\n"))
    assert "Einheit" in result.errors[0] and "Steuer" in result.errors[1]


def test_existing_items_by_sku_or_name_are_skipped(shop):
    result = import_items(shop.company, csv(ITEM_HEADER + ";;beratung;LS;1;\n"))
    assert result.created == 0 and len(result.skipped) == 1


def test_item_export_reads_back(client, shop):
    client.force_login(shop.user)
    exported = client.get("/leistungen/export.csv").content
    Item.objects.filter(company=shop.company).delete()
    result = import_items(shop.company, exported)
    assert result.errors == [] and Item.objects.get().net_price == Decimal("80")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1234,50", "1234.50"), ("1.234,50", "1234.50"), ("1234.50", "1234.50"), ("1.234", "1234"), ("80 €", "80")],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


def test_read_table_keeps_line_numbers_across_blank_lines():
    assert [line for line, _ in read_table(b"Name\nA\n\n\nB\n", {"Name"})] == [2, 5]


# ---- pages and roles ---------------------------------------------------------------------------


def test_upload_page_imports_and_redirects(client, shop):
    client.force_login(shop.user)
    response = client.post("/kunden/import/", {"file": upload(csv("Name\nPer Upload\n"))})
    assert response.status_code == 302 and Customer.objects.filter(name="Per Upload").exists()
    page = client.post("/leistungen/import/", {"file": upload(b"Bezeichnung\nX\n")}).content.decode()
    assert "Es fehlen Spalten" in page


def test_import_page_escapes_hostile_values(client, shop):
    client.force_login(shop.user)
    page = client.post("/kunden/import/", {"file": upload(csv("Name;Rolle\nX;<script>alert(1)</script>\n"))})
    assert b"<script>alert(1)</script>" not in page.content and b"&lt;script&gt;" in page.content


def test_drafts_only_role_may_export_but_not_import(client, shop, django_user_model):
    helper = django_user_model.objects.create_user("hilfe")
    helper.groups.add(Group.objects.get_or_create(name=DRAFTS_ONLY_GROUP)[0])
    client.force_login(helper)
    assert client.get("/kunden/export.csv").status_code == 200
    assert client.post("/kunden/import/", {"file": upload(csv("Name\nVerboten\n"))}).status_code == 403
    assert client.post("/leistungen/import/", {"file": upload(csv(ITEM_HEADER))}).status_code == 403
    assert not Customer.objects.filter(name="Verboten").exists()


def test_login_required(client, shop):
    for url in ("/kunden/export.csv", "/kunden/import/", "/leistungen/export.csv", "/leistungen/import/"):
        assert client.get(url).status_code == 302
