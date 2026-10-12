"""KI-Assistent: ask, follow progress, review the result; configure the language model."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from skontio.assistant.models import AssistantRun, LlmConfig
from skontio.assistant.providers import ProviderError, connection_test
from skontio.assistant.services import current_config, get_provider, save_config, start_run
from skontio.documents.models import Document
from skontio.web.support import admin_required, company_view, is_htmx


@company_view
def assistant(request):
    if request.method == "POST":
        try:
            run = start_run(request.company, request.user, request.POST.get("prompt", ""))
        except ProviderError as exc:
            messages.error(request, str(exc))
            return redirect("assistant")
        return redirect(f"/assistent/{run.pk}/")
    runs = AssistantRun.objects.filter(company=request.company, user=request.user)[:20]
    config = current_config()
    return render(request, "web/assistant/index.html", {"runs": runs, "configured": config["kind"] != "none"})


@company_view
def assistant_run(request, pk: int):
    run = get_object_or_404(AssistantRun, pk=pk, company=request.company, user=request.user)
    documents = Document.objects.filter(pk__in=run.documents, company=request.company)
    steps = [m for m in run.messages if m["role"] == "tool"]
    context = {"run": run, "documents": documents, "steps": steps, "pending": run.status in ("queued", "running")}
    template = "web/assistant/_run.html" if is_htmx(request) else "web/assistant/run.html"
    return render(request, template, context)


class ConfigForm(forms.Form):
    kind = forms.ChoiceField(label="Anbieter", choices=LlmConfig.Kind.choices)
    base_url = forms.CharField(
        label="Adresse", required=False, help_text="Leer = Standard (Ollama: http://127.0.0.1:11434/v1)"
    )
    model = forms.CharField(label="Modell", required=False, help_text="z. B. qwen3:8b oder claude-sonnet-5-5")
    api_key = forms.CharField(
        label="API-Schlüssel",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leer lassen, um den gespeicherten Schlüssel zu behalten.",
    )
    remove_key = forms.BooleanField(label="Gespeicherten Schlüssel entfernen", required=False)
    timeout = forms.IntegerField(label="Timeout je Schritt (Sekunden)", min_value=10, max_value=600, initial=300)


@admin_required
def assistant_settings(request):
    config = current_config()
    form = ConfigForm(request.POST or None, initial={k: config[k] for k in ("kind", "base_url", "model", "timeout")})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        api_key = "" if data["remove_key"] else (data["api_key"] or None)
        try:
            save_config(
                kind=data["kind"],
                base_url=data["base_url"],
                model=data["model"],
                api_key=api_key,
                timeout=data["timeout"],
                user=request.user,
            )
        except ProviderError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Einstellungen gespeichert.")
            return redirect("assistant_settings")
    context = {"form": form, "source": config["source"], "has_key": bool(config["api_key"]), "kind": config["kind"]}
    return render(request, "web/assistant/settings.html", context)


@admin_required
@require_POST
def assistant_test(request):
    try:
        provider = get_provider()
        if provider is None:
            raise ProviderError("Kein Anbieter eingestellt.")
        result = connection_test(provider)
    except ProviderError as exc:
        messages.error(request, f"Verbindungstest fehlgeschlagen: {exc}")
    else:
        tools = "Werkzeuge funktionieren" if result["tools"] else "Modell ruft keine Werkzeuge auf – ungeeignet"
        models = ", ".join(result["models"][:5]) or "–"
        messages.success(request, f"Verbunden ({result['latency_ms']} ms). {tools}. Modelle: {models}")
    return redirect("assistant_settings")
