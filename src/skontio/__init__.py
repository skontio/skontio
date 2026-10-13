"""Skontio – Open-Source-Rechnungen und CRM für Selbstständige."""

import os
import sys

__version__ = "0.2.0"


def main() -> None:
    """``skontio <command>`` – thin wrapper around Django's management commands."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "skontio.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(["skontio", *sys.argv[1:]])
