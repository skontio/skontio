#!/usr/bin/env bash
# Download the official e-invoice validators used by the test suite and CI.
#   KoSIT validator + XRechnung configuration  -> XRechnung / EN 16931 business rules
#   Mustang CLI (bundles veraPDF)              -> ZUGFeRD/Factur-X PDF/A-3 + XML
# Versions are pinned; checksums are verified on every download (TOFU file below).
set -euo pipefail

cd "$(dirname "$0")"
CACHE=.cache
mkdir -p "$CACHE"

KOSIT_VERSION=1.6.3
XRECHNUNG_CONFIG=xrechnung-3.0.2-validator-configuration-2026-08-31
XRECHNUNG_TAG=v2026-08-31
MUSTANG_VERSION=2.26.0

download() {
  local url=$1 target=$2
  if [[ ! -f "$CACHE/$target" ]]; then
    echo "-> $target"
    curl -fsSL --retry 3 -o "$CACHE/$target.part" "$url"
    mv "$CACHE/$target.part" "$CACHE/$target"
  fi
}

download "https://github.com/itplr-kosit/validator/releases/download/v${KOSIT_VERSION}/validator-${KOSIT_VERSION}-standalone.jar" \
  "kosit-validator.jar"
download "https://github.com/itplr-kosit/validator-configuration-xrechnung/releases/download/${XRECHNUNG_TAG}/${XRECHNUNG_CONFIG}.zip" \
  "xrechnung-config.zip"
download "https://github.com/ZUGFeRD/mustangproject/releases/download/core-${MUSTANG_VERSION}/Mustang-CLI-${MUSTANG_VERSION}.jar" \
  "mustang-cli.jar"

if [[ -f checksums.sha256 ]]; then
  (cd "$CACHE" && sha256sum --check --quiet ../checksums.sha256)
else
  (cd "$CACHE" && sha256sum kosit-validator.jar xrechnung-config.zip mustang-cli.jar) > checksums.sha256
  echo "Recorded checksums in tools/validators/checksums.sha256 – commit this file."
fi

if [[ ! -d "$CACHE/xrechnung-config" ]]; then
  mkdir -p "$CACHE/xrechnung-config"
  (cd "$CACHE/xrechnung-config" && python3 -m zipfile -e ../xrechnung-config.zip .)
fi
echo "Validators ready in tools/validators/$CACHE"
