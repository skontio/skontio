from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_triggers(sender, using, **kwargs):
    from django.db import connections

    from skontio.documents.locking import install_lock_triggers

    install_lock_triggers(connections[using])


class DocumentsConfig(AppConfig):
    name = "skontio.documents"
    verbose_name = "Dokumente"

    def ready(self):
        post_migrate.connect(_install_triggers, sender=self, dispatch_uid="skontio_documents_lock_triggers")
