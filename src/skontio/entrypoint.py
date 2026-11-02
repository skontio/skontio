"""Container entrypoint (``skontio-entrypoint``) – in Python, so the image needs no shell.

Roles:
  web     – apply migrations (with a backup first), then serve UI, API, OAuth and /mcp via Granian
  worker  – background jobs (``skontio worker``)
  *       – any other arguments go to the ``skontio`` management CLI
"""

from __future__ import annotations

import os
import subprocess
import sys


def granian_argv(workers: str) -> list[str]:
    if not workers.isdigit() or not 1 <= int(workers) <= 32:
        raise SystemExit("SKONTIO_WEB_WORKERS muss eine Zahl zwischen 1 und 32 sein")
    return [
        "granian", "--interface", "asginl",  # Django has no ASGI lifespan support
        "--host", "0.0.0.0", "--port", "8000",  # noqa: S104 – inside the container; published to the LAN only
        "--workers", workers, "--respawn-failed-workers",
        "--static-path-route", "/static", "--static-path-mount", "/app/static",
        "skontio.asgi:application",
    ]  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv) or ["web"]
    role = args[0]
    if role == "web":
        # backs up an existing database automatically before applying migrations (plan §9)
        subprocess.run(["skontio", "upgrade"], check=True)  # noqa: S603, S607 – fixed argv, no shell
        command = granian_argv(os.environ.get("SKONTIO_WEB_WORKERS", "2"))
    elif role == "worker":
        command = ["skontio", "worker"]
    else:
        command = ["skontio", *args]
    os.execvp(command[0], command)  # noqa: S606 – replaces this process; tini stays PID 1
