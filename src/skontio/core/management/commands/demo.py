"""Fill an empty installation with sample data for local testing (compose.dev.yaml, tools/smoke.sh).

Finalized invoices can never be deleted, so this refuses to run unless SKONTIO_DEMO=1 is set and
no company exists yet.
"""

import datetime as dt
import os
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.models import Customer
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.payments.services import record_payment
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime


class Command(BaseCommand):
    help = "Legt Beispieldaten an (nur für lokale Testinstanzen, braucht SKONTIO_DEMO=1)."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")
        parser.add_argument("--password", default="demo")

    def handle(self, *args, username, password, **options):
        if os.environ.get("SKONTIO_DEMO") != "1":
            raise CommandError("Nur für Testinstanzen: SKONTIO_DEMO=1 setzen")
        if Company.objects.exists():
            self.stdout.write("Es gibt schon eine Firma – keine Beispieldaten angelegt.")
            return
        today = timezone.localdate()
        with transaction.atomic():
            user_model = get_user_model()
            user = user_model.objects.filter(username=username).first()
            if user is None:
                user = user_model.objects.create_superuser(username, password=password)
            company = Company.objects.create(
                name="Demo Selbstständig",
                street="Musterweg 1",
                postcode="12345",
                city="Musterstadt",
                tax_number="12/345/67890",
                email="rechnung@example.org",
                contact_name="Demo Inhaber",
                iban="DE02120300000000202051",
                side_business=True,
            )
            start_regime(company, Regime.SMALL_BUSINESS, dt.date(today.year, 1, 1), confirmed_by=user)
            org = create_customer(
                company,
                name="Beispiel GmbH",
                street="Hauptstr. 5",
                postcode="10115",
                city="Berlin",
                email="buchhaltung@beispiel.example",
            )
            create_customer(
                company,
                kind=Customer.Kind.PERSON,
                name="Erika Mustermann",
                street="Privatweg 7",
                postcode="50667",
                city="Köln",
                email="erika@privat.example",
            )
            consulting = create_item(company, name="Beratung", unit="HUR", net_price=Decimal("80"))
            setup = create_item(company, name="Einrichtung", unit="C62", net_price=Decimal("250"))

            quote = create_draft(company, Document.Type.QUOTE, org, issue_date=today, created_by=user)
            add_line_from_item(quote, setup, quantity=Decimal("1"))
            add_line_from_item(quote, consulting, quantity=Decimal("4"))

            invoice = create_draft(
                company, Document.Type.INVOICE, org, issue_date=today, service_date=today, created_by=user
            )
            add_line_from_item(invoice, consulting, quantity=Decimal("2.5"))
        invoice = finalize(invoice)
        record_payment(invoice, Decimal("100"), today, created_by=user)
        self.stdout.write(
            self.style.SUCCESS(f"Beispieldaten angelegt: Login {username}/{password}, Rechnung {invoice.number}")
        )
