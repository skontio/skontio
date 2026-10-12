from __future__ import annotations

import hashlib
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


def _event_hash(prev_hash: str, fields: dict) -> str:
    return hashlib.sha256((prev_hash + json.dumps(fields, sort_keys=True, default=str)).encode()).hexdigest()


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
    """Return problems: altered events, gaps (deleted events) and a cut-off end of the chain."""
    problems: list[str] = []
    expected_prev, expected_seq = GENESIS, 1
    for event in AuditEvent.objects.order_by("seq").iterator():
        if event.seq != expected_seq:
            problems.append(f"Einträge {expected_seq}–{event.seq - 1} fehlen")
        if event.prev_hash != expected_prev:
            problems.append(f"#{event.seq}: Verkettung zum Vorgänger unterbrochen")
        if _event_hash(event.prev_hash, _hashed_fields(event)) != event.hash:
            problems.append(f"#{event.seq}: Inhalt wurde verändert")
        expected_prev, expected_seq = event.hash, event.seq + 1
    head = AuditHead.objects.filter(pk=1).first()
    if head and (head.seq, head.hash) != (expected_seq - 1, expected_prev):
        problems.append(f"Ende der Kette fehlt: erwartet #{head.seq}, vorhanden #{expected_seq - 1}")
    return problems
