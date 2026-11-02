"""Postausgang: every mail Skontio sent (documents and reminders), with filters and CSV export."""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlencode

from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from skontio.mail.models import MailLog
from skontio.web.csvexport import csv_response
from skontio.web.document_views import _iso_date
from skontio.web.support import company_view

STATUS = {"versendet": MailLog.Status.SENT, "fehlgeschlagen": MailLog.Status.FAILED}
MAX_QUERY = 100
PAGE = 200


def _moment(day: dt.date, *, end: bool) -> dt.datetime:
    return timezone.make_aware(dt.datetime.combine(day, dt.time.max if end else dt.time.min))


@company_view
def outbox(request):
    mine = MailLog.objects.filter(document__company=request.company)
    logs = mine.select_related("document", "sent_by")
    status = request.GET.get("status", "")
    if status in STATUS:
        logs = logs.filter(status=STATUS[status])
    else:
        status = ""
    query = " ".join(request.GET.get("q", "").split())[:MAX_QUERY]
    if query:
        # recipients are a JSON list; a text match on the stored JSON is enough for a search box
        logs = logs.filter(Q(subject__icontains=query) | Q(document__number__icontains=query) | Q(to__icontains=query))
    start, end = _iso_date(request.GET.get("von", "")), _iso_date(request.GET.get("bis", ""))
    if start:
        logs = logs.filter(sent_at__gte=_moment(start, end=False))
    if end:
        logs = logs.filter(sent_at__lte=_moment(end, end=True))
    if request.GET.get("csv"):
        return csv_response(
            f"postausgang-{timezone.localdate().isoformat()}.csv",
            ["Zeitpunkt", "Status", "An", "Kopie", "Betreff", "Dokument", "Anhänge", "Fehler"],
            (
                [
                    timezone.localtime(log.sent_at).strftime("%Y-%m-%d %H:%M"),
                    log.get_status_display(),
                    ", ".join(log.to),
                    ", ".join(log.cc),
                    log.subject,
                    log.document.number,
                    ", ".join(str(a.get("name", "")) for a in log.attachments),
                    log.error,
                ]
                for log in logs.iterator()
            ),
        )
    params = {"status": status, "q": query, "von": start or "", "bis": end or ""}
    return render(
        request,
        "web/outbox.html",
        {
            "logs": logs[:PAGE],
            "status": status,
            "q": query,
            "von": start,
            "bis": end,
            "failed": mine.filter(status=MailLog.Status.FAILED).count(),
            "csv_query": urlencode({k: v for k, v in params.items() if v} | {"csv": "1"}),
        },
    )
