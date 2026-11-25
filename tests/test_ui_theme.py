"""Oberfläche: „Standard“ ist die Voreinstellung, „Modern“ lädt zusätzlich modern.css (Einstellungen)."""

from __future__ import annotations

import datetime as dt

import pytest

from skontio.audit.models import AuditEvent
from skontio.core.models import Company
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime

URL = "/einstellungen/oberflaeche/"


@pytest.fixture
def company(db):
    company = Company.objects.create(name="Muster Selbstständig", street="Weg 1", postcode="12345", city="Stadt")
    start_regime(company, Regime.SMALL_BUSINESS, dt.date(2024, 1, 1))
    return company


@pytest.fixture
def admin(client, company, django_user_model):
    client.force_login(django_user_model.objects.create_user("inhaber", is_staff=True, is_superuser=True))
    return client


def page(client) -> str:
    return client.get("/").content.decode()


def test_standard_is_the_default_and_loads_no_modern_css(admin, company):
    assert company.ui_theme == "standard"
    body = page(admin)
    assert "web/app.css" in body and "web/modern.css" not in body and 'class="hero"' not in body


def test_modern_adds_stylesheet_after_app_css_and_the_hero(admin, company):
    assert admin.post(URL, {"ui_theme": "modern"}).status_code == 302
    company.refresh_from_db()
    assert company.ui_theme == "modern"
    body = page(admin)
    assert body.index("web/app.css") < body.index("web/modern.css") and 'class="hero"' in body


def test_can_switch_back(admin, company):
    admin.post(URL, {"ui_theme": "modern"})
    admin.post(URL, {"ui_theme": "standard"})
    assert "web/modern.css" not in page(admin)


def test_unknown_value_is_rejected(admin, company):
    admin.post(URL, {"ui_theme": "neon"})
    company.refresh_from_db()
    assert company.ui_theme == "standard"


def test_settings_page_shows_current_choice(admin, company):
    admin.post(URL, {"ui_theme": "modern"})
    body = admin.get(URL).content.decode()
    assert 'value="modern" checked' in body and 'value="standard" checked' not in body


def test_change_is_audited_but_saving_the_same_value_is_not(admin, company):
    admin.post(URL, {"ui_theme": "modern"})
    admin.post(URL, {"ui_theme": "modern"})
    event = AuditEvent.objects.get(action="company.ui_theme_changed")
    assert event.data == {"from": "standard", "to": "modern"}


def test_only_admins_may_change_it(client, company, django_user_model):
    client.force_login(django_user_model.objects.create_user("mitarbeit"))
    assert client.get(URL).status_code == 403
    assert client.post(URL, {"ui_theme": "modern"}).status_code == 403
    company.refresh_from_db()
    assert company.ui_theme == "standard"


def test_login_page_follows_the_company_setting(client, company):
    company.ui_theme = "modern"
    company.save()
    assert "web/modern.css" in client.get("/auth/local/").content.decode()
