from django.core.management.base import BaseCommand
from django.utils import timezone

from skontio.core.models import Company
from skontio.recurring.services import generate_due


class Command(BaseCommand):
    help = "Legt fällige wiederkehrende Rechnungen als Entwürfe an (für einen täglichen Cron-Job)."

    def handle(self, *args, **options):
        today = timezone.localdate()
        total = 0
        for company in Company.objects.all():
            drafts = generate_due(company, today=today)
            total += len(drafts)
            for draft in drafts:
                period = f"{draft.service_period_start:%d.%m.%Y}–{draft.service_period_end:%d.%m.%Y}"
                self.stdout.write(f"{company.name}: Entwurf {draft.pk} für {draft.customer.name} ({period})")
        self.stdout.write(self.style.SUCCESS(f"{total} Entwurf/Entwürfe angelegt."))
