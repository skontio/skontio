"""Database-level immutability of finalized documents (GoBD: Unveränderbarkeit).

Installed idempotently after every ``migrate`` (post_migrate), because Django rebuilds
SQLite tables on many schema changes and a rebuilt table loses its triggers.
There is deliberately no bypass (ADR 0001).
"""

from __future__ import annotations

import re

SQLITE = [
    """
    CREATE TRIGGER IF NOT EXISTS skontio_document_no_update BEFORE UPDATE ON documents_document
    FOR EACH ROW WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skontio_document_no_delete BEFORE DELETE ON documents_document
    FOR EACH ROW WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skontio_line_no_insert BEFORE INSERT ON documents_documentline
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = NEW.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skontio_line_no_update BEFORE UPDATE ON documents_documentline
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = OLD.document_id) <> 'draft'
                   OR (SELECT status FROM documents_document WHERE id = NEW.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skontio_line_no_delete BEFORE DELETE ON documents_documentline
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = OLD.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    # prepayment deductions of a Schlussrechnung are part of the finalized document
    """
    CREATE TRIGGER IF NOT EXISTS skontio_deduction_no_insert BEFORE INSERT ON documents_prepaymentdeduction
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = NEW.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skontio_deduction_no_update BEFORE UPDATE ON documents_prepaymentdeduction
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = OLD.document_id) <> 'draft'
                   OR (SELECT status FROM documents_document WHERE id = NEW.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS skontio_deduction_no_delete BEFORE DELETE ON documents_prepaymentdeduction
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = OLD.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
]

POSTGRES = [
    """
    CREATE OR REPLACE FUNCTION skontio_document_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'skontio: document % is finalized and immutable', OLD.number
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$;
    """,
    "DROP TRIGGER IF EXISTS skontio_document_guard ON documents_document;",
    """
    CREATE TRIGGER skontio_document_guard BEFORE UPDATE OR DELETE ON documents_document
    FOR EACH ROW EXECUTE FUNCTION skontio_document_guard();
    """,
    """
    CREATE OR REPLACE FUNCTION skontio_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        parent_status text;
    BEGIN
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            SELECT status INTO parent_status FROM documents_document WHERE id = OLD.document_id;
            IF parent_status <> 'draft' THEN
                RAISE EXCEPTION 'skontio: document is finalized and immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            SELECT status INTO parent_status FROM documents_document WHERE id = NEW.document_id;
            IF parent_status <> 'draft' THEN
                RAISE EXCEPTION 'skontio: document is finalized and immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END IF;
        RETURN OLD;
    END $$;
    """,
    "DROP TRIGGER IF EXISTS skontio_line_guard ON documents_documentline;",
    """
    CREATE TRIGGER skontio_line_guard BEFORE INSERT OR UPDATE OR DELETE ON documents_documentline
    FOR EACH ROW EXECUTE FUNCTION skontio_line_guard();
    """,
    # same guard function: it only looks at document_id
    "DROP TRIGGER IF EXISTS skontio_deduction_guard ON documents_prepaymentdeduction;",
    """
    CREATE TRIGGER skontio_deduction_guard BEFORE INSERT OR UPDATE OR DELETE ON documents_prepaymentdeduction
    FOR EACH ROW EXECUTE FUNCTION skontio_line_guard();
    """,
]


SQLITE_DROP = [
    f"DROP TRIGGER IF EXISTS {name};"
    for name in (
        "skontio_document_no_update",
        "skontio_document_no_delete",
        "skontio_line_no_insert",
        "skontio_line_no_update",
        "skontio_line_no_delete",
        "skontio_deduction_no_insert",
        "skontio_deduction_no_update",
        "skontio_deduction_no_delete",
    )
]
POSTGRES_DROP = [
    "DROP TRIGGER IF EXISTS skontio_line_guard ON documents_documentline;",
    "DROP TRIGGER IF EXISTS skontio_deduction_guard ON documents_prepaymentdeduction;",
    "DROP TRIGGER IF EXISTS skontio_document_guard ON documents_document;",
]


TABLE = re.compile(r"\bON (documents_\w+)")


def _execute(connection, statements: list[str]) -> None:
    # Older migrations call this too, before later tables exist: skip triggers for missing tables.
    tables = set(connection.introspection.table_names())
    with connection.cursor() as cursor:
        for statement in statements:
            if (match := TABLE.search(statement)) and match.group(1) not in tables:
                continue
            # params=None: no placeholder parsing, so PL/pgSQL's RAISE '%' stays literal
            cursor.execute(statement, None)


def drop_lock_triggers(connection) -> None:
    """Only for schema migrations of the document tables; reinstall in the *same* migration."""
    _execute(connection, {"sqlite": SQLITE_DROP, "postgresql": POSTGRES_DROP}[connection.vendor])


def schema_change_operations(*operations):
    """Wrap schema operations on document tables: drop triggers, alter, reinstall – one transaction."""
    from django.db import migrations

    return [
        migrations.RunPython(lambda apps, se: drop_lock_triggers(se.connection), migrations.RunPython.noop),
        *operations,
        migrations.RunPython(lambda apps, se: install_lock_triggers(se.connection), migrations.RunPython.noop),
    ]


def install_lock_triggers(connection) -> None:
    statements = {"sqlite": SQLITE, "postgresql": POSTGRES}.get(connection.vendor)
    if statements is None:
        raise NotImplementedError(f"No lock triggers for {connection.vendor}")
    _execute(connection, statements)
