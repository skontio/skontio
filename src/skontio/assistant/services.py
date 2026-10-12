"""In-app assistant (plan §7.4): the same tool registry as MCP, driven by a configurable LLM.

* Runs as the logged-in user with ``actor_kind=assistant``: only the draft/read tools exist, every
  call is audited, and drafts are marked "KI-Entwurf, bitte prüfen" until a person reviews them.
* Cloud providers see private customers pseudonymised (as MCP clients do); a local Ollama does not.
* Requests run in a background thread (N150: slow local models) with a step counter for progress.
"""

from __future__ import annotations

import json
import threading
import time

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from skontio.assistant import providers
from skontio.assistant.models import AssistantRun, LlmConfig
from skontio.assistant.providers import ProviderError, ToolSpec, Turn
from skontio.audit.models import AuditEvent
from skontio.audit.services import record
from skontio.core.crypto import SealError, seal, unseal
from skontio.mcp.protocol import INSTRUCTIONS
from skontio.tools.registry import ScopeMissing, ToolContext, ToolError, UnknownTool, available_tools, call_tool

MAX_STEPS = 8
MAX_SECONDS = 600  # small local models on an N150 need minutes per step
SEAL_PURPOSE = "llm-api-key"
DRAFT_TYPES = {"quote", "invoice", "order_confirmation", "partial_invoice", "final_invoice"}


def current_config() -> dict:
    """Effective configuration: environment (SKONTIO_LLM_*) wins over the settings page."""
    if settings.LLM_ENV.get("kind"):
        return dict(settings.LLM_ENV) | {"source": "env"}
    row = LlmConfig.objects.first()
    if row is None:
        return {"kind": "none", "base_url": "", "model": "", "api_key": "", "timeout": 300, "source": "default"}
    try:
        api_key = unseal(row.api_key_sealed, purpose=SEAL_PURPOSE) if row.api_key_sealed else ""
    except SealError:
        api_key = ""  # secret_key changed since saving – the key has to be entered again
    return {
        "kind": row.kind,
        "base_url": row.base_url,
        "model": row.model,
        "api_key": api_key,
        "timeout": row.timeout,
        "source": "settings",
    }


def get_provider():
    config = current_config()
    return providers.build(
        config["kind"],
        base_url=config["base_url"],
        model=config["model"],
        api_key=config["api_key"],
        timeout=float(config["timeout"]),
    )


def save_config(*, kind: str, base_url: str, model: str, api_key: str | None, timeout: int, user=None) -> LlmConfig:
    """``api_key=None`` keeps the stored key, ``""`` removes it."""
    if kind not in LlmConfig.Kind.values:
        raise ProviderError("Unbekannter Anbieter.")
    if base_url:
        providers.check_url(base_url)
    with transaction.atomic():
        row = LlmConfig.objects.select_for_update().first() or LlmConfig()
        row.kind, row.base_url, row.model = kind, base_url.strip(), model.strip()
        row.timeout = max(10, min(timeout, 600))
        if api_key is not None:
            row.api_key_sealed = seal(api_key, purpose=SEAL_PURPOSE) if api_key else ""
        row.updated_by = user
        row.save()
        record("assistant.config", actor=user, data={"kind": kind, "base_url": row.base_url, "model": row.model})
    return row


def system_prompt(today) -> str:
    return (
        f"{INSTRUCTIONS}\n\nDu bist der Assistent in Skontio. Heute ist der {today:%d.%m.%Y}. Antworte auf Deutsch, "
        "kurz und konkret. Suche Kunden und Leistungen, bevor du Entwürfe anlegst. Gibt es mehrere passende Treffer "
        "oder fehlen Angaben, frage nach statt zu raten. Nenne am Ende, was du angelegt hast, und dass der Entwurf "
        "in Skontio geprüft werden muss."
    )


def tool_specs(scopes: frozenset[str]) -> list[ToolSpec]:
    specs = []
    for tool in available_tools(scopes):
        definition = tool.definition()
        specs.append(ToolSpec(definition["name"], definition["description"], definition["inputSchema"]))
    return specs


def _context(run: AssistantRun, provider_name: str) -> ToolContext:
    return ToolContext(
        company=run.company,
        user=run.user,
        scopes=frozenset(settings.MCP_SCOPES),
        actor_kind=AuditEvent.ActorKind.ASSISTANT,
        client_id=f"assistant:{provider_name}",
        pseudonymise_persons=settings.MCP_PSEUDONYMISE_PERSONS and provider_name != "ollama",
    )


def _execute(ctx: ToolContext, name: str, arguments: dict) -> tuple[str, dict | None]:
    try:
        result = call_tool(ctx, name, arguments if isinstance(arguments, dict) else {})
    except (ToolError, UnknownTool) as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False), None
    except ScopeMissing as exc:
        return json.dumps({"error": f"Nicht erlaubt ({exc.scope})"}, ensure_ascii=False), None
    return json.dumps(result, ensure_ascii=False, default=str)[:20_000], result


def run_assistant(run: AssistantRun, provider=None) -> AssistantRun:
    try:
        provider = provider or get_provider()
    except ProviderError as exc:
        provider, run.error = None, str(exc)
    if provider is None:
        run.status = AssistantRun.Status.FAILED
        run.error = run.error or "Kein KI-Anbieter eingerichtet (Einstellungen → Assistent)."
        run.finished_at = timezone.now()
        run.save()
        return run
    run.status, run.provider = AssistantRun.Status.RUNNING, getattr(provider, "name", "")
    run.messages = run.messages or [{"role": "user", "content": run.prompt}]
    run.save(update_fields=["status", "provider", "messages"])
    ctx, specs = _context(run, run.provider), tool_specs(frozenset(settings.MCP_SCOPES))
    system, started = system_prompt(timezone.localdate()), time.monotonic()
    try:
        for _ in range(MAX_STEPS):
            if time.monotonic() - started > MAX_SECONDS:
                raise ProviderError("Zeitlimit überschritten.")
            turn: Turn = provider.complete(system, run.messages, specs)
            calls = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls]
            run.messages.append({"role": "assistant", "content": turn.text, "tool_calls": calls})
            run.steps += 1
            if not calls:
                run.answer = turn.text
                break
            for call in calls:
                content, result = _execute(ctx, call["name"], call["arguments"])
                run.messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": content}
                )
                document_id = result.get("document_id") if result else None
                if document_id and result.get("type") in DRAFT_TYPES and document_id not in run.documents:
                    run.documents.append(document_id)
            run.save(update_fields=["messages", "steps", "documents"])
        else:
            run.answer = "Ich habe nach mehreren Schritten aufgehört – bitte die Anfrage genauer fassen."
        run.status = AssistantRun.Status.DONE
    except ProviderError as exc:
        run.status, run.error = AssistantRun.Status.FAILED, str(exc)
    run.finished_at = timezone.now()
    run.save()
    return run


def _run_in_thread(run_pk: int) -> None:
    close_old_connections()
    try:
        run_assistant(AssistantRun.objects.select_related("company", "user").get(pk=run_pk))
    except Exception as exc:  # noqa: BLE001 – a background thread must record every failure
        AssistantRun.objects.filter(pk=run_pk).update(
            status=AssistantRun.Status.FAILED, error=f"Interner Fehler: {exc}"[:2000], finished_at=timezone.now()
        )
    finally:
        close_old_connections()


def start_run(company, user, prompt: str) -> AssistantRun:
    prompt = prompt.strip()
    if not prompt:
        raise ProviderError("Bitte eine Frage oder Aufgabe eingeben.")
    run = AssistantRun.objects.create(company=company, user=user, prompt=prompt[:4000])
    if settings.ASSISTANT_SYNC:
        return run_assistant(run)
    transaction.on_commit(lambda: threading.Thread(target=_run_in_thread, args=(run.pk,), daemon=True).start())
    return run
