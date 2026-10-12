"""One tool registry for MCP (Phase 1b) and the assistant (Phase 2), plan §7.

Rules enforced here, not in the individual tools:
* every tool needs a scope; calls without it raise ``ScopeMissing``
* inputs are validated with Pydantic (field limits, ``extra="forbid"``); errors become ``ToolError``
  messages the model can act on
* every call – successful or not – is written to the hash-chained audit log, marked as AI-initiated,
  with a digest of the arguments instead of clear text
* each call runs in one transaction: a failing tool leaves no partial drafts behind
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from pydantic import BaseModel, ValidationError

from skontio.audit import services as audit
from skontio.audit.models import AuditEvent
from skontio.documents.models import DocumentLocked
from skontio.documents.services import DocumentError


class ToolError(Exception):
    """A business or input error the model can correct (reported with ``isError: true``)."""


class UnknownTool(LookupError):
    pass


class ScopeMissing(PermissionError):
    def __init__(self, scope: str):
        super().__init__(f"Scope {scope!r} fehlt")
        self.scope = scope


@dataclass(frozen=True)
class ToolContext:
    company: Any
    user: Any
    scopes: frozenset[str]
    actor_kind: str = AuditEvent.ActorKind.MCP
    client_id: str = ""
    pseudonymise_persons: bool = True


@dataclass
class Out:
    """Tool result plus what the audit log should reference."""

    data: dict
    entity: str = ""
    entity_id: Any = ""


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    scope: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], Out]
    read_only: bool = True
    idempotent: bool = True
    annotations: dict = field(default_factory=dict)

    def definition(self) -> dict:
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": schema,
            "annotations": {
                "title": self.title,
                "readOnlyHint": self.read_only,
                "destructiveHint": False,  # no tool deletes, finalizes, sends or books anything
                "idempotentHint": self.idempotent,
                "openWorldHint": False,
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(*, name: str, title: str, scope: str, input_model: type[BaseModel], read_only: bool = True, idempotent=None):
    def register(handler):
        if name in REGISTRY:
            raise RuntimeError(f"Tool {name} ist doppelt registriert")
        REGISTRY[name] = Tool(
            name=name,
            title=title,
            description=" ".join((handler.__doc__ or "").split()),
            scope=scope,
            input_model=input_model,
            handler=handler,
            read_only=read_only,
            idempotent=read_only if idempotent is None else idempotent,
        )
        return handler

    return register


def available_tools(scopes: frozenset[str]) -> list[Tool]:
    """Tools the granted scopes permit, in a stable order (prompt-cache friendly)."""
    _load()
    return [t for name, t in sorted(REGISTRY.items()) if t.scope in scopes]


def get_tool(name: str) -> Tool:
    _load()
    try:
        return REGISTRY[name]
    except KeyError:
        raise UnknownTool(name) from None


def _format_validation(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors(include_url=False)[:10]:
        location = ".".join(str(p) for p in error["loc"]) or "Eingabe"
        parts.append(f"{location}: {error['msg']}")
    return "Ungültige Eingabe – " + "; ".join(parts)


def call_tool(ctx: ToolContext, name: str, arguments: dict | None) -> dict:
    tool_ = get_tool(name)
    if tool_.scope not in ctx.scopes:
        raise ScopeMissing(tool_.scope)
    arguments = arguments or {}
    audit_args = {
        "actor": ctx.user,
        "actor_kind": ctx.actor_kind,
        "company": ctx.company,
        "client_id": ctx.client_id,
        "tool_name": name,
        "request_digest": audit.digest(arguments),
    }
    try:
        args = tool_.input_model.model_validate(arguments)
        with transaction.atomic():
            out = tool_.handler(ctx, args)
            audit.record(f"tool.{name}", entity=out.entity, entity_id=out.entity_id, **audit_args)
    except ValidationError as exc:
        error = ToolError(_format_validation(exc))
    except (ToolError, DocumentError, DocumentLocked, ValueError, ObjectDoesNotExist) as exc:
        error = exc if isinstance(exc, ToolError) else ToolError(str(exc))
    else:
        return out.data
    audit.record(f"tool.{name}.rejected", data={"error": str(error)[:500]}, **audit_args)
    raise error


def _load() -> None:
    """Import the tool modules once so their @tool decorators run."""
    from skontio.tools import catalog, customers, documents, drafts, reports  # noqa: F401
