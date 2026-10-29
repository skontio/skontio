import pytest
from django.core.management import CommandError, call_command

from skontio.core.models import Company
from skontio.documents.models import Document
from skontio.payments.models import Payment


@pytest.mark.django_db
def test_demo_requires_opt_in(monkeypatch):
    monkeypatch.delenv("SKONTIO_DEMO", raising=False)
    with pytest.raises(CommandError, match="SKONTIO_DEMO=1"):
        call_command("demo")
    assert not Company.objects.exists()


@pytest.mark.django_db
def test_demo_seeds_an_empty_installation_once(monkeypatch, django_user_model):
    monkeypatch.setenv("SKONTIO_DEMO", "1")
    call_command("demo", username="probe", password="geheim")

    assert django_user_model.objects.get(username="probe").check_password("geheim")
    invoice = Document.objects.get(doc_type=Document.Type.INVOICE)
    assert invoice.status == Document.Status.FINALIZED
    assert invoice.renditions.filter(kind="zugferd_pdf").exists()
    assert Document.objects.filter(doc_type=Document.Type.QUOTE, status=Document.Status.DRAFT).exists()
    assert Payment.objects.filter(document=invoice).count() == 1

    call_command("demo")  # second run leaves existing data alone
    assert Company.objects.count() == 1
    assert Document.objects.count() == 2
