from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from django.db import transaction
from django.utils import timezone

from skontio.audit.models import AuditEvent, AuditHead

GENESIS = "0" * 64
AI_KINDS = {AuditEvent.ActorKind.MCP, AuditEvent.ActorKind.ASSISTANT}

SQLITE_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS skontio_audit_no_update BEFORE UPDATE ON audit_auditevent
    BEGIN SELECT RAISE(ABORT, 'skontio: audit events are append-only'); END;
    """,
]
POSTGRES_TRIGGERS = [
    """
    CREATE OR REPLACE FUNCTION skontio_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'skontio: audit events are append-only' USING ERRCODE = 'integrity_constraint_violation';
    END $$;
    """,
    "DROP TRIGGER IF EXISTS skontio_audit_guard ON audit_auditevent;",
    """
    CREATE TRIGGER skontio_audit_guard BEFORE UPDATE ON audit_auditevent
    FOR EACH ROW EXECUTE FUNCTION skontio_audit_guard();
    """,
]


def install_audit_triggers(connection) -> None:
    """UPDATE is refused; deletions are detected by the hash chain (verify_chain)."""
    statements = {"sqlite": SQLITE_TRIGGERS, "postgresql": POSTGRES_TRIGGERS}.get(connection.vendor, [])
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement, params=None)


def digest(payload: Any) -> str:
    """SHA-256 over canonical JSON – stored instead of clear-text requests from AI clients."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


KEYED_MARKER = "audit.chain_keyed"


def _payload(prev_hash: str, fields: dict) -> bytes:
    return (prev_hash + json.dumps(fields, sort_keys=True, default=str)).encode()


def _legacy_hash(prev_hash: str, fields: dict) -> str:
    """Plain SHA-256 of chains written before the key was introduced."""
    return hashlib.sha256(_payload(prev_hash, fields)).hexdigest()


def _event_hash(prev_hash: str, fields: dict) -> str:
    """HMAC-SHA256 with a key derived from SKONTIO_SECRET_KEY: someone who can only write to the database
    cannot recompute the chain after changing or deleting an event."""
    from skontio.core.crypto import _key

    return hmac.new(_key("audit-chain"), _payload(prev_hash, fields), hashlib.sha256).hexdigest()


def _hashed_fields(event: AuditEvent) -> dict:
    return {
        "seq": event.seq,
        "created_at": event.created_at.isoformat(),
        "company": event.company_id,
        "actor": event.actor_id,
        "actor_kind": event.actor_kind,
        "client_id": event.client_id,
        "tool_name": event.tool_name,
        "ai_initiated": event.ai_initiated,
        "action": event.action,
        "entity": event.entity,
        "entity_id": event.entity_id,
        "data": event.data,
        "request_digest": event.request_digest,
    }


def record(
    action: str,
    *,
    actor=None,
    actor_kind: str = AuditEvent.ActorKind.USER,
    company=None,
    entity: str = "",
    entity_id: Any = "",
    data: dict | None = None,
    client_id: str = "",
    tool_name: str = "",
    request_digest: str = "",
) -> AuditEvent:
    """Append one event. Runs inside the caller's transaction, so it is rolled back together with it."""
    with transaction.atomic():
        head, _ = AuditHead.objects.select_for_update().get_or_create(pk=1)
        event = AuditEvent(
            seq=head.seq + 1,
            created_at=timezone.now(),
            company=company,
            actor=actor if getattr(actor, "pk", None) else None,
            actor_kind=actor_kind,
            client_id=client_id[:255],
            tool_name=tool_name,
            ai_initiated=actor_kind in AI_KINDS,
            action=action,
            entity=entity,
            entity_id=str(entity_id or ""),
            data=json.loads(json.dumps(data or {}, default=str)),  # what is hashed is what is stored
            request_digest=request_digest,
            prev_hash=head.hash,
        )
        event.hash = _event_hash(head.hash, _hashed_fields(event))
        event.save()
        head.seq, head.hash = event.seq, event.hash
        head.save(update_fields=["seq", "hash"])
    return event


def verify_chain() -> list[str]:
    """Return problems: altered events, gaps (deleted events) and a cut-off end of the chain.

    Events are keyed (HMAC). Only events before the keyed marker (written once when the key was
    introduced) may use the old plain hash – and the keyed marker pins them through its own link."""
    problems: list[str] = []
    expected_prev, expected_seq = GENESIS, 1
    marker = AuditEvent.objects.filter(action=KEYED_MARKER).order_by("seq").values_list("seq", flat=True).first()
    for event in AuditEvent.objects.order_by("seq").iterator():
        if event.seq != expected_seq:
            problems.append(f"Einträge {expected_seq}–{event.seq - 1} fehlen")
        if event.prev_hash != expected_prev:
            problems.append(f"#{event.seq}: Verkettung zum Vorgänger unterbrochen")
        legacy_allowed = marker is not None and event.seq < marker
        fields = _hashed_fields(event)
        valid = hmac.compare_digest(_event_hash(event.prev_hash, fields), event.hash) or (
            legacy_allowed and _legacy_hash(event.prev_hash, fields) == event.hash
        )
        if not valid:
            problems.append(f"#{event.seq}: Inhalt wurde verändert")
        expected_prev, expected_seq = event.hash, event.seq + 1
    head = AuditHead.objects.filter(pk=1).first()
    if head and (head.seq, head.hash) != (expected_seq - 1, expected_prev):
        problems.append(f"Ende der Kette fehlt: erwartet #{head.seq}, vorhanden #{expected_seq - 1}")
    return problems


def key_chain() -> AuditEvent | None:
    """Switch an existing plain chain to keyed hashes by appending the marker (idempotent)."""
    if not AuditEvent.objects.exists() or AuditEvent.objects.filter(action=KEYED_MARKER).exists():
        return None
    return record(KEYED_MARKER, actor_kind=AuditEvent.ActorKind.SYSTEM, data={"algorithm": "HMAC-SHA256"})


def head() -> dict:
    """The end of the chain – stored outside the database (backup manifest) as an anchor."""
    current = AuditHead.objects.filter(pk=1).first()
    return {"seq": current.seq, "hash": current.hash} if current else {"seq": 0, "hash": GENESIS}


def verify_anchor(anchor: dict) -> list[str]:
    """Compare an anchor taken earlier (e.g. from an off-site backup) with today's chain."""
    seq, expected = int(anchor.get("seq", 0)), str(anchor.get("hash", ""))
    if seq == 0:
        return []
    event = AuditEvent.objects.filter(seq=seq).first()
    if event is None:
        return [f"Anker #{seq}: Eintrag fehlt"]
    if not hmac.compare_digest(event.hash, expected):
        return [f"Anker #{seq}: Protokoll wurde seit der Sicherung umgeschrieben"]
    return []
