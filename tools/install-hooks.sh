#!/bin/sh
# One-time setup: use the hooks from tools/git-hooks and create the local list of private patterns.
set -eu
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath tools/git-hooks
chmod +x tools/git-hooks/*
patterns="$(git rev-parse --git-dir)/private-patterns"
if [ ! -e "$patterns" ]; then
  printf '%s\n' 'mailbox\.org' > "$patterns"
  echo "Angelegt: $patterns – dort weitere Muster eintragen (eigene Adresse, Hostnamen, IPs), eines pro Zeile."
fi
echo "Hooks aktiv (core.hooksPath=tools/git-hooks)."
command -v gitleaks >/dev/null 2>&1 || echo "Tipp: gitleaks installieren, dann prüft der Hook auch Passwörter und Schlüssel."
