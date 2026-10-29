#!/usr/bin/env bash
# Lokaler End-to-End-Test im echten Docker-Image (compose.dev.yaml):
# baut das Image, startet Web und Worker, legt Beispieldaten an, meldet sich an,
# ruft die wichtigsten Seiten ab, lädt PDF/ZUGFeRD herunter, prüft Archiv und Backup.
#
#   tools/smoke.sh               # SQLite, räumt danach auf
#   tools/smoke.sh --postgres    # mit Postgres 17
#   tools/smoke.sh --keep        # Container laufen lassen (http://localhost:8099, Login demo/demo)
#   tools/smoke.sh --no-build    # vorhandenes Image skontio:dev verwenden
set -euo pipefail

cd "$(dirname "$0")/.."

postgres=0 keep=0 build=--build
for arg in "$@"; do
  case "$arg" in
    --postgres) postgres=1 ;;
    --keep) keep=1 ;;
    --no-build) build= ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
  esac
done

export SKONTIO_DEV_PORT="${SKONTIO_DEV_PORT:-8099}"
base="http://localhost:${SKONTIO_DEV_PORT}"
compose=(docker compose -f compose.dev.yaml -p skontio-smoke)
if [ "$postgres" = 1 ]; then
  export SKONTIO_DATABASE_URL=postgres://skontio:skontio@postgres:5432/skontio
  compose+=(--profile pg)
fi

work="$(mktemp -d)"
jar="$work/cookies"
failures=0

cleanup() {
  status=$?
  if [ "$status" != 0 ] || [ "$failures" != 0 ]; then
    echo "--- Logs (letzte 60 Zeilen) ---"
    "${compose[@]}" logs --tail 60 || true
  fi
  if [ "$keep" = 1 ]; then
    echo "Läuft weiter: $base/auth/local/ (demo/demo) · stoppen: ${compose[*]} down -v"
  else
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFEHL\033[0m %s\n' "$1"; failures=$((failures + 1)); }
manage() { "${compose[@]}" exec -T skontio skontio "$@"; }

echo "==> Container bauen und starten (Port $SKONTIO_DEV_PORT, $([ "$postgres" = 1 ] && echo Postgres || echo SQLite))"
"${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
# shellcheck disable=SC2086
"${compose[@]}" up -d $build --wait --wait-timeout 180

echo "==> Beispieldaten"
manage demo

echo "==> HTTP"
if curl -sf "$base/healthz" >/dev/null; then ok "/healthz"; else fail "/healthz"; fi

curl -sf -c "$jar" -b "$jar" "$base/auth/local/" -o /dev/null
csrf="$(awk '$6 == "csrftoken" {print $7}' "$jar")"
login_status="$(curl -s -o /dev/null -w '%{http_code}' -c "$jar" -b "$jar" \
  -H "Origin: $base" -H "Referer: $base/auth/local/" \
  --data-urlencode "csrfmiddlewaretoken=$csrf" --data-urlencode username=demo --data-urlencode password=demo \
  "$base/auth/local/")"
if [ "$login_status" = 302 ]; then ok "Login demo/demo"; else fail "Login (HTTP $login_status)"; fi

for page in / /kunden/ /leistungen/ /dokumente/ /ausgaben/ /ausgaben/belege/ /eingang/ /auswertungen/euer/ \
            /auswertungen/euer.csv /auswertungen/ustva/ /mahnwesen/ /wiederkehrend/ /bank/ /assistent/ \
            /einstellungen/steuerstatus/ /einstellungen/vorlagen/ /einstellungen/sicherheit/ \
            /einstellungen/ki-zugriff/ /manifest.webmanifest /sw.js; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" "$base$page")"
  if [ "$code" = 200 ]; then ok "GET $page"; else fail "GET $page (HTTP $code)"; fi
done

ids="$(manage shell --no-imports -c "
from skontio.documents.models import Document
inv = Document.objects.filter(doc_type='invoice', status='finalized').first()
quote = Document.objects.filter(doc_type='quote').first()
print(inv.pk, quote.pk)
" | tail -1)"
read -r invoice_id quote_id <<<"$ids"

check_pdf() {  # url, label → leaves the file in $work/out
  local file="$work/out" code
  code="$(curl -s -o "$file" -w '%{http_code}' -b "$jar" "$base$1")"
  if [ "$code" != 200 ] || [ "$(head -c 5 "$file")" != "%PDF-" ]; then fail "$2 (HTTP $code)"; return 1; fi
  ok "$2 ($(wc -c <"$file") Bytes)"
}
check_pdf "/dokumente/$quote_id/vorschau.pdf" "Entwurfsvorschau Angebot" || true
if check_pdf "/dokumente/$invoice_id/datei/zugferd_pdf/" "Rechnung als ZUGFeRD-PDF"; then
  # extract the embedded XML with the same library the app uses
  if "${compose[@]}" exec -T skontio python -c "
import sys
from facturx import get_xml_from_pdf
name, xml = get_xml_from_pdf(sys.stdin.buffer.read(), check_xsd=True)
assert name == 'factur-x.xml' and b'CrossIndustryInvoice' in xml, name
" <"$work/out"; then ok "ZUGFeRD-XML eingebettet und XSD-gültig"; else fail "ZUGFeRD-XML"; fi
fi

echo "==> Betrieb"
if manage verify_archive >/dev/null; then ok "Archiv-Prüfsummen"; else fail "verify_archive"; fi
if manage backup >/dev/null; then ok "Backup erstellt"; else fail "backup"; fi
if manage doctor backup >/dev/null; then ok "doctor backup"; else fail "doctor backup"; fi
if [ "$("${compose[@]}" ps --status running -q skontio-worker | wc -l)" = 1 ]; then
  ok "Worker läuft"
else
  fail "Worker läuft nicht"
fi

echo
if [ "$failures" = 0 ]; then echo "Smoke-Test bestanden."; else echo "$failures Prüfung(en) fehlgeschlagen."; exit 1; fi
