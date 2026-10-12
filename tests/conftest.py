import pytest


@pytest.fixture(autouse=True)
def _isolated_archive(tmp_path, settings):
    """Finalizing writes PDFs/XML to the archive; never let tests touch the real one."""
    settings.ARCHIVE_DIR = tmp_path / "archive"


ALL_MCP_SCOPES = frozenset(
    "catalog:read catalog:write customers:read customers:write "
    "drafts:read drafts:write documents:read reports:read".split()
)


@pytest.fixture
def shop(db):
    """A company under Regelbesteuerung with one business customer, one private customer and one service."""
    import datetime as dt
    from decimal import Decimal
    from types import SimpleNamespace

    from django.contrib.auth import get_user_model

    from skontio.catalog.services import create_item
    from skontio.core.models import Company
    from skontio.crm.models import Customer
    from skontio.crm.services import create_customer
    from skontio.tax.engine import Regime
    from skontio.taxstatus.services import start_regime

    company = Company.objects.create(
        name="Muster Selbstständig",
        street="Weg 1",
        postcode="12345",
        city="Stadt",
        tax_number="1/2/3",
        iban="DE02120300000000202051",
        email="rechnung@example.org",
    )
    start_regime(company, Regime.STANDARD, dt.date(2024, 1, 1))
    return SimpleNamespace(
        company=company,
        user=get_user_model().objects.create_user("inhaber", is_staff=True),
        org=create_customer(company, name="Kunde GmbH", street="W 1", postcode="2", city="K", email="a@kunde.example"),
        person=create_customer(
            company,
            kind=Customer.Kind.PERSON,
            name="Erika Mustermann",
            street="Privatweg 7",
            postcode="3",
            city="P",
            email="erika@privat.example",
            phone="+49 170 1234567",
        ),
        item=create_item(company, name="Beratung", unit="HUR", net_price=Decimal("80")),
    )


@pytest.fixture
def ctx(shop):
    from skontio.tools.registry import ToolContext

    return ToolContext(company=shop.company, user=shop.user, scopes=ALL_MCP_SCOPES, client_id="claude-test")


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Rate-limit counters live in the cache; token ids repeat across rolled-back tests."""
    from django.core.cache import cache

    cache.clear()
