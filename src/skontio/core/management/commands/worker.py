import signal
import threading

from django.core.management.base import BaseCommand

from skontio.ops.scheduler import run_due, serve


class Command(BaseCommand):
    help = "Hintergrund-Worker: nächtliches Backup und wiederkehrende Rechnungen (Container-Rolle »worker«)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="fällige Aufgaben einmal ausführen und beenden")

    def handle(self, *args, once, **options):
        if once:
            for run in run_due():
                self.stdout.write(f"{run.job}: {run.get_status_display()} – {run.message}")
            return
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())  # PID 1 in Docker ignores SIGTERM without a handler
        serve(stop)
