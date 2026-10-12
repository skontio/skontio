"""KI-Assistent: Schleife mit Fake-LLM, Adapter (OpenAI-kompatibel, Anthropic), Konfiguration, Oberfläche."""

from __future__ import annotations

import json

import httpx
import pytest

from skontio.assistant import providers
from skontio.assistant.models import AssistantRun, LlmConfig
from skontio.assistant.providers import Fake, ProviderError, ToolCall, ToolSpec, Turn, check_url
from skontio.assistant.services import current_config, run_assistant, save_config, start_run
from skontio.audit.models import AuditEvent
from skontio.documents.models import Document


@pytest.fixture(autouse=True)
def _sync(settings):
    settings.ASSISTANT_SYNC = True
    settings.LLM_ENV = {"kind": "", "base_url": "", "model": "", "api_key": "", "timeout": 120}


def invoice_script(shop) -> list[Turn]:
    create = ToolCall(
        "c2",
        "invoice_draft_create",
        {
            "customer_number": shop.org.number,
            "idempotency_key": "assistant-test-0001",
            "service_date": "2026-10-09",
            "lines": [{"item_id": shop.item.pk, "quantity": "3"}],
        },
    )
    return [
        Turn(tool_calls=[ToolCall("c1", "customer_search", {"query": "Kunde"})]),
        Turn(text="Ich lege den Entwurf an.", tool_calls=[create]),
        Turn(text="Entwurf für Kunde GmbH angelegt – bitte in Skontio prüfen."),
    ]


def new_run(shop, prompt="x") -> AssistantRun:
    return AssistantRun.objects.create(company=shop.company, user=shop.user, prompt=prompt)


class TestLoop:
    def test_creates_an_ai_draft_through_the_tools(self, shop):
        fake = Fake(invoice_script(shop))
        run = run_assistant(new_run(shop, "Rechnung an Kunde GmbH"), provider=fake)
        assert run.status == "done" and run.steps == 3 and run.answer.startswith("Entwurf")
        (doc,) = Document.objects.filter(pk__in=run.documents)
        assert doc.origin == Document.Origin.ASSISTANT and doc.ai_review_required
        event = AuditEvent.objects.get(tool_name="invoice_draft_create")
        assert event.actor_kind == "assistant" and event.client_id == "assistant:fake" and event.ai_initiated
        offered = fake.requests[0]["tools"]
        assert "invoice_draft_create" in offered and not any("finalize" in name for name in offered)
        tool_messages = [m for m in run.messages if m["role"] == "tool"]
        assert tool_messages[0]["name"] == "customer_search" and shop.org.number in tool_messages[0]["content"]

    def test_private_customers_are_pseudonymised_for_cloud_models(self, shop):
        fake = Fake([Turn(tool_calls=[ToolCall("c1", "customer_search", {"query": "Erika"})]), Turn(text="ok")])
        run = run_assistant(new_run(shop), fake)
        assert "Erika" not in run.messages[2]["content"] and "Privatperson" in run.messages[2]["content"]

    def test_tool_errors_go_back_to_the_model(self, shop):
        calls = [ToolCall("c1", "invoice_finalize", {}), ToolCall("c2", "catalog_get", {"item_id": 0})]
        fake = Fake([Turn(tool_calls=calls), Turn(text="Das kann ich nicht.")])
        run = run_assistant(new_run(shop), fake)
        errors = [json.loads(m["content"])["error"] for m in run.messages if m["role"] == "tool"]
        assert len(errors) == 2 and "invoice_finalize" in errors[0]
        assert run.status == "done" and run.answer == "Das kann ich nicht."

    def test_step_limit(self, shop):
        endless = Fake([Turn(tool_calls=[ToolCall(f"c{i}", "catalog_search", {"query": "x"})]) for i in range(20)])
        run = run_assistant(new_run(shop), endless)
        assert run.steps == 8 and "aufgehört" in run.answer

    def test_provider_errors(self, shop):
        class Broken(Fake):
            def complete(self, system, messages, tools):
                raise ProviderError("KI-Anbieter nicht erreichbar: timeout")

        run = run_assistant(new_run(shop), Broken([]))
        assert run.status == "failed" and "timeout" in run.error

    def test_without_provider(self, shop):
        run = start_run(shop.company, shop.user, "Hallo")
        assert run.status == "failed" and "Kein KI-Anbieter" in run.error


def mock_http(monkeypatch, handler):
    seen = []
    original = httpx.Client

    def record(request):
        seen.append(request)
        return handler(request)

    def client(**kwargs):
        assert kwargs.get("follow_redirects") is False
        return original(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(providers.httpx, "Client", client)
    return seen


SPEC = [ToolSpec("catalog_search", "Suche", {"type": "object", "properties": {"query": {"type": "string"}}})]
HISTORY = [
    {"role": "user", "content": "Suche Beratung"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "a", "name": "catalog_search", "arguments": {"query": "B"}}],
    },
    {"role": "tool", "tool_call_id": "a", "name": "catalog_search", "content": '{"items": []}'},
]


class TestAdapters:
    def test_openai_compatible(self, monkeypatch):
        call = {"id": "x1", "type": "function", "function": {"name": "catalog_search", "arguments": '{"query": "Web"}'}}
        answer = {"choices": [{"message": {"content": None, "tool_calls": [call]}}]}
        seen = mock_http(monkeypatch, lambda request: httpx.Response(200, json=answer))
        turn = providers.build("ollama", model="qwen3:8b").complete("System", HISTORY, SPEC)
        assert turn.tool_calls == [ToolCall("x1", "catalog_search", {"query": "Web"})]
        assert str(seen[0].url) == "http://127.0.0.1:11434/v1/chat/completions"
        body = json.loads(seen[0].content)
        assert body["tools"][0]["function"]["name"] == "catalog_search"
        assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
        assert json.loads(body["messages"][2]["tool_calls"][0]["function"]["arguments"]) == {"query": "B"}

    def test_anthropic(self, monkeypatch):
        answer = {
            "content": [
                {"type": "text", "text": "Hier"},
                {"type": "tool_use", "id": "t1", "name": "catalog_search", "input": {"query": "X"}},
            ]
        }
        seen = mock_http(monkeypatch, lambda request: httpx.Response(200, json=answer))
        turn = providers.build("anthropic", api_key="sk-test").complete("System", HISTORY, SPEC)
        assert turn.text == "Hier" and turn.tool_calls[0].arguments == {"query": "X"}
        request = seen[0]
        assert request.headers["x-api-key"] == "sk-test" and str(request.url).endswith("/v1/messages")
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-5-5" and body["system"] == "System"
        result = {"type": "tool_result", "tool_use_id": "a", "content": '{"items": []}'}
        assert body["messages"][2]["content"][0] == result

    def test_tool_call_written_as_text_is_understood(self, monkeypatch):
        """Qwen in Ollama answers {"name": …, "arguments": …} as text – seen in a live test."""
        text = '{"name": "catalog_search", "arguments": {"query": "Kunde GmbH"}}'
        answer = {"choices": [{"message": {"role": "assistant", "content": text}}]}
        mock_http(monkeypatch, lambda request: httpx.Response(200, json=answer))
        turn = providers.build("ollama", model="qwen2.5-coder:7b").complete("s", HISTORY, SPEC)
        assert turn.tool_calls == [ToolCall("inline_0", "catalog_search", {"query": "Kunde GmbH"})]
        assert turn.text == ""

    def test_unknown_names_in_text_stay_text(self, monkeypatch):
        text = '{"name": "invoice_finalize", "arguments": {}}'
        answer = {"choices": [{"message": {"role": "assistant", "content": text}}]}
        mock_http(monkeypatch, lambda request: httpx.Response(200, json=answer))
        turn = providers.build("ollama", model="m").complete("s", HISTORY, SPEC)
        assert turn.tool_calls == [] and turn.text == text

    def test_redirects_are_not_followed(self, monkeypatch):
        mock_http(monkeypatch, lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/"}))
        with pytest.raises(ProviderError, match="HTTP 302"):
            providers.build("ollama", model="m").complete("s", [{"role": "user", "content": "x"}], [])

    def test_urls_and_keys(self):
        with pytest.raises(ProviderError):
            check_url("file:///etc/passwd")
        with pytest.raises(ProviderError, match="API-Schlüssel"):
            providers.build("anthropic")
        with pytest.raises(ProviderError, match="Modell"):
            providers.build("openai_compatible")
        assert providers.build("none") is None


class TestConfig:
    def test_key_is_stored_sealed(self, shop):
        save_config(kind="anthropic", base_url="", model="", api_key="sk-secret", timeout=90, user=shop.user)
        assert "sk-secret" not in LlmConfig.objects.get().api_key_sealed
        assert current_config()["api_key"] == "sk-secret"
        save_config(kind="anthropic", base_url="", model="", api_key=None, timeout=90)
        assert current_config()["api_key"] == "sk-secret"  # None keeps the key
        save_config(kind="anthropic", base_url="", model="", api_key="", timeout=90)
        assert current_config()["api_key"] == ""

    def test_environment_wins(self, settings, shop):
        save_config(kind="anthropic", base_url="", model="", api_key="sk", timeout=90)
        settings.LLM_ENV = {
            "kind": "ollama",
            "base_url": "http://nas:11434/v1",
            "model": "m",
            "api_key": "",
            "timeout": 60,
        }
        assert current_config()["kind"] == "ollama" and current_config()["source"] == "env"


class TestWeb:
    def test_ask_and_follow_the_result(self, client, shop, monkeypatch):
        monkeypatch.setattr("skontio.assistant.services.get_provider", lambda: Fake(invoice_script(shop)))
        client.force_login(shop.user)
        response = client.post("/assistent/", {"prompt": "Rechnung an Kunde GmbH über 3 Stunden"})
        run = AssistantRun.objects.get()
        assert response["Location"] == f"/assistent/{run.pk}/"
        page = client.get(f"/assistent/{run.pk}/").content.decode()
        assert "bitte in Skontio prüfen" in page and "KI-Entwurf" in page and "every 2s" not in page
        fragment = client.get(f"/assistent/{run.pk}/", headers={"HX-Request": "true"}).content.decode()
        assert fragment.lstrip().startswith('<div id="run"')

    def test_runs_are_private_per_user(self, client, shop, django_user_model):
        run = new_run(shop, "geheim")
        client.force_login(django_user_model.objects.create_user("andere"))
        assert client.get(f"/assistent/{run.pk}/").status_code == 404

    def test_settings_and_connection_test(self, client, shop, monkeypatch):
        client.force_login(shop.user)
        client.post(
            "/einstellungen/assistent/",
            {"kind": "ollama", "base_url": "http://192.168.1.10:11434/v1", "model": "qwen3:8b", "timeout": "120"},
        )
        assert LlmConfig.objects.get().base_url == "http://192.168.1.10:11434/v1"
        result = {"models": ["qwen3:8b"], "tools": True, "latency_ms": 42}
        monkeypatch.setattr("skontio.web.assistant_views.connection_test", lambda provider: result)
        page = client.post("/einstellungen/assistent/test/", follow=True).content.decode()
        assert "Verbunden (42 ms)" in page and "Werkzeuge funktionieren" in page
