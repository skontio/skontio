#!/usr/bin/env bash
# Update-Test: startet eine ältere, veröffentlichte Version mit Beispieldaten, wechselt auf das neue Image
# (skontio:dev) und prüft, dass nichts verloren geht oder sich verändert:
#   festgeschriebene Rechnungen (Nummer, Betrag), archivierte PDFs/XML (Prüfsummen), Kunden, Audit-Kette,
#   Archiv, Backup vor dem Update, Anmeldung und wichtigste Seiten.
#
#   tools/upgrade-test.sh                                   # von ghcr.io/skontio/skontio:0.2 auf skontio:dev
#   tools/upgrade-test.sh --from ghcr.io/skontio/skontio:edge
#   tools/upgrade-test.sh --postgres                        # mit Postgres 17
#   tools/upgrade-test.sh --no-build                        # vorhandenes skontio:dev verwenden
set -euo pipefail

cd "$(dirname "$0")/.."

from=ghcr.io/skontio/skontio:0.2 postgres=0 build=1
while [ $# -gt 0 ]; do
  case "$1" in
    --from) from="$2"; shift ;;
    --postgres) postgres=1 ;;
    --no-build) build=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
  esac
  shift
done

export SKONTIO_DEV_PORT="${SKONTIO_DEV_PORT:-8098}"
base="http://localhost:${SKONTIO_DEV_PORT}"
compose=(docker compose -f compose.dev.yaml -p skontio-upgrade)
if [ "$postgres" = 1 ]; then
  export SKONTIO_DATABASE_URL=postgres://skontio:skontio@postgres:5432/skontio
  compose+=(--profile pg)
fi

work="$(mktemp -d)"
failures=0
cleanup() {
  status=$?
  if [ "$status" != 0 ] || [ "$failures" != 0 ]; then
    echo "--- Logs (letzte 60 Zeilen) ---"
    "${compose[@]}" logs --tail 60 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFEHL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
manage() { "${compose[@]}" exec -T skontio skontio "$@"; }

# what must survive the update, as JSON (works with old and new versions: only long-standing fields)
state() {
  manage shell --no-imports -c "
import json
from skontio.audit.models import AuditEvent
from skontio.crm.models import Customer
from django.db.migrations.recorder import MigrationRecorder
from skontio.documents.models import Document
docs = Document.objects.exclude(status='draft').order_by('pk')
print(json.dumps({
    'migrations': MigrationRecorder.Migration.objects.count(),
    'documents': [[d.doc_type, d.number, str(d.gross_total), sorted(r.blob.sha256 for r in d.renditions.all())]
                  for d in docs],
    'customers': sorted(Customer.objects.values_list('number', flat=True)),
    'audit_events': AuditEvent.objects.count(),
}, sort_keys=True))
" | tail -1
}

if [ "$build" = 1 ]; then
  echo "==> Neues Image bauen (skontio:dev)"
  docker build -q -t skontio:dev . >/dev/null
fi

echo "==> Alte Version starten: $from"
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
docker pull -q "$from" >/dev/null
SKONTIO_IMAGE="$from" "${compose[@]}" up -d --no-build --wait --wait-timeout 180 skontio
SKONTIO_IMAGE="$from" "${compose[@]}" up -d --no-build skontio-worker
if manage help demo >/dev/null 2>&1; then
  manage demo >/dev/null
else  # older releases have no demo command: the services below exist since the first release
  manage shell --no-imports -c "
import datetime as dt
from decimal import Decimal
from django.contrib.auth import get_user_model
from skontio.catalog.services import create_item
from skontio.core.models import Company
from skontio.crm.services import create_customer
from skontio.documents.models import Document
from skontio.documents.services import add_line_from_item, create_draft, finalize
from skontio.tax.engine import Regime
from skontio.taxstatus.services import start_regime
get_user_model().objects.create_user('demo', password='demo', is_staff=True)
company = Company.objects.create(name='Update-Test GmbH', street='Weg 1', postcode='12345', city='Stadt',
                                 tax_number='12/345/67890', iban='DE02120300000000202051', email='a@example.org')
start_regime(company, Regime.STANDARD, dt.date(2026, 1, 1))
customer = create_customer(company, name='Kunde AG', street='Platz 2', postcode='54321', city='Dorf')
item = create_item(company, name='Beratung', unit='HUR', net_price=Decimal('90'))
day = dt.date(2026, 3, 2)
for doc_type in (Document.Type.QUOTE, Document.Type.INVOICE, Document.Type.INVOICE):
    draft = create_draft(company, doc_type, customer, issue_date=day, service_date=day)
    add_line_from_item(draft, item, quantity=Decimal('3'))
    finalize(draft)
" >/dev/null
fi
old_version="$(manage shell --no-imports -c 'import skontio; print(skontio.__version__)' | tail -1)"
state >"$work/before.json"
count="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))['documents']))" "$work/before.json")"
ok "Version $old_version mit Beispieldaten ($count festgeschriebene Dokumente)"

echo "==> Auf das neue Image wechseln (Daten bleiben)"
"${compose[@]}" up -d --no-build --wait --wait-timeout 300 skontio
"${compose[@]}" up -d --no-build skontio-worker
new_version="$(manage shell --no-imports -c 'import skontio; print(skontio.__version__)' | tail -1)"
ok "läuft jetzt mit $new_version"
state >"$work/after.json"

echo "==> Vergleich"
if python3 - "$work/before.json" "$work/after.json" <<'EOF'
import json, sys
before, after = (json.load(open(path)) for path in sys.argv[1:])
problems = []
if after["documents"] != before["documents"]:
    problems.append("festgeschriebene Dokumente oder ihre archivierten Dateien haben sich verändert")
if after["customers"] != before["customers"]:
    problems.append("Kunden fehlen oder haben andere Nummern")
if after["audit_events"] < before["audit_events"]:
    problems.append("Einträge im Änderungsprotokoll fehlen")
for problem in problems:
    print("   ", problem)
sys.exit(1 if problems else 0)
EOF
then ok "Dokumente, Archivdateien, Kunden und Protokoll unverändert"; else fail "Daten verändert"; fi
if manage verify_audit >/dev/null; then ok "Audit-Kette gültig"; else fail "verify_audit"; fi
if manage verify_archive >/dev/null; then ok "Archiv-Prüfsummen"; else fail "verify_archive"; fi
migrated="$(python3 -c "import json,sys; a,b=(json.load(open(p)) for p in sys.argv[1:]); print(b['migrations']-a['migrations'])" "$work/before.json" "$work/after.json")"
if [ "$migrated" -gt 0 ]; then
  if "${compose[@]}" exec -T skontio python -c "
import pathlib, sys
sys.exit(0 if any(pathlib.Path('/backups').glob('*vor-update*')) else 1)"; then
    ok "Backup vor dem Update angelegt ($migrated Migrationen)"
  else
    fail "kein Backup vor dem Update"
  fi
fi

jar="$work/cookies"
curl -sf -c "$jar" -b "$jar" "$base/auth/local/" -o /dev/null
csrf="$(awk '$6 == "csrftoken" {print $7}' "$jar")"
login="$(curl -s -o /dev/null -w '%{http_code}' -c "$jar" -b "$jar" -H "Origin: $base" -H "Referer: $base/auth/local/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode username=demo --data-urlencode password=demo "$base/auth/local/")"
if [ "$login" = 302 ]; then ok "Anmeldung mit bestehendem Konto"; else fail "Anmeldung (HTTP $login)"; fi
for page in / /dokumente/ /kunden/ /ausgaben/ /bank/ /auswertungen/euer/ /einstellungen/vorlagen/ /einrichtung/assistent/; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" "$base$page")"
  if [ "$code" = 200 ]; then ok "GET $page"; else fail "GET $page (HTTP $code)"; fi
done
if [ "$("${compose[@]}" ps --status running -q skontio-worker | wc -l)" = 1 ]; then ok "Worker läuft"; else fail "Worker läuft nicht"; fi

echo
if [ "$failures" = 0 ]; then echo "Update-Test bestanden ($old_version → $new_version)."; else echo "$failures Prüfung(en) fehlgeschlagen."; exit 1; fi
