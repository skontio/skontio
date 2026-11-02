from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _install_triggers(sender, using, **kwargs):
    from django.db import connections

    from skontio.audit.services import install_audit_triggers

    install_audit_triggers(connections[using])


def _key_chain(sender, using, **kwargs):
    """Existing installations switch to the keyed (HMAC) chain once, on the first migrate after the update."""
    from skontio.audit.services import key_chain

    key_chain()


class AuditConfig(AppConfig):
    name = "skontio.audit"
    verbose_name = "Änderungsprotokoll"

    def ready(self):
        post_migrate.connect(_install_triggers, sender=self, dispatch_uid="skontio_audit_triggers")
        post_migrate.connect(_key_chain, sender=self, dispatch_uid="skontio_audit_key_chain")
