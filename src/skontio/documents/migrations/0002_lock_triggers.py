"""Database-level immutability of finalized documents (GoBD: Unveränderbarkeit).

The ORM already refuses changes; these triggers also stop raw SQL, admin shells
and buggy migrations. There is deliberately no bypass.
"""

from django.db import migrations

SQLITE_UP = [
    """
    CREATE TRIGGER skontio_document_no_update BEFORE UPDATE ON documents_document
    FOR EACH ROW WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER skontio_document_no_delete BEFORE DELETE ON documents_document
    FOR EACH ROW WHEN OLD.status <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER skontio_line_no_insert BEFORE INSERT ON documents_documentline
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = NEW.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER skontio_line_no_update BEFORE UPDATE ON documents_documentline
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = OLD.document_id) <> 'draft'
                   OR (SELECT status FROM documents_document WHERE id = NEW.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
    """
    CREATE TRIGGER skontio_line_no_delete BEFORE DELETE ON documents_documentline
    FOR EACH ROW WHEN (SELECT status FROM documents_document WHERE id = OLD.document_id) <> 'draft'
    BEGIN SELECT RAISE(ABORT, 'skontio: document is finalized and immutable'); END;
    """,
]

SQLITE_DOWN = [
    f"DROP TRIGGER IF EXISTS {name};"
    for name in (
        "skontio_document_no_update",
        "skontio_document_no_delete",
        "skontio_line_no_insert",
        "skontio_line_no_update",
        "skontio_line_no_delete",
    )
]

POSTGRES_UP = [
    """
    CREATE FUNCTION skontio_document_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'skontio: document % is finalized and immutable', OLD.number
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END $$;
    """,
    """
    CREATE TRIGGER skontio_document_guard BEFORE UPDATE OR DELETE ON documents_document
    FOR EACH ROW EXECUTE FUNCTION skontio_document_guard();
    """,
    """
    CREATE FUNCTION skontio_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
    """
    CREATE TRIGGER skontio_line_guard BEFORE INSERT OR UPDATE OR DELETE ON documents_documentline
    FOR EACH ROW EXECUTE FUNCTION skontio_line_guard();
    """,
]

POSTGRES_DOWN = [
    "DROP TRIGGER IF EXISTS skontio_line_guard ON documents_documentline;",
    "DROP FUNCTION IF EXISTS skontio_line_guard();",
    "DROP TRIGGER IF EXISTS skontio_document_guard ON documents_document;",
    "DROP FUNCTION IF EXISTS skontio_document_guard();",
]


def _run(statements_by_vendor):
    def run(apps, schema_editor):
        statements = statements_by_vendor.get(schema_editor.connection.vendor)
        if statements is None:
            raise NotImplementedError(f"No lock triggers for {schema_editor.connection.vendor}")
        for statement in statements:
            # params=None: no placeholder parsing, so PL/pgSQL's RAISE '%' stays literal
            schema_editor.execute(statement, params=None)

    return run


class Migration(migrations.Migration):
    dependencies = [("documents", "0001_initial")]

    operations = [
        migrations.RunPython(
            _run({"sqlite": SQLITE_UP, "postgresql": POSTGRES_UP}),
            _run({"sqlite": SQLITE_DOWN, "postgresql": POSTGRES_DOWN}),
        ),
    ]
