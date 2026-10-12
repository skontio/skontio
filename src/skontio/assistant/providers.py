"""LLM port and adapters (plan §7.4): ``none``, ``ollama``, ``openai_compatible``, ``anthropic`` – plus ``Fake``.

A small port on top of httpx instead of a framework: the assistant only needs "send the
conversation and the tool definitions, get text and tool calls back". Ollama is used via its
OpenAI-compatible endpoint. Redirects are never followed (SSRF) and only http(s) URLs are accepted.

Conversation format (provider-neutral):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}, …]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Protocol

import httpx

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_URLS = {
    "ollama": "http://127.0.0.1:11434/v1",
    "openai_compatible": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}
DEFAULT_MODELS = {"ollama": "qwen3:8b", "anthropic": "claude-sonnet-5-5"}


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: dict


class Provider(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn: ...

    def models(self) -> list[str]: ...


def check_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError(f"Ungültige Adresse für den KI-Anbieter: {url!r}")
    return url.strip().rstrip("/")


def _request(method: str, url: str, *, timeout: float, headers: dict, payload: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.request(method, url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"KI-Anbieter nicht erreichbar: {exc}") from exc
    if response.status_code >= 300:  # redirects are refused, not followed
        raise ProviderError(f"KI-Anbieter antwortet mit HTTP {response.status_code}: {response.text[:300]}")
    try:
        return response.json()
    except ValueError as exc:
        raise ProviderError("KI-Anbieter liefert kein JSON.") from exc


def _inline_tool_calls(text: str, names: set[str]) -> list[ToolCall]:
    """Some local models (e.g. Qwen in Ollama) write the tool call as JSON into the text instead of the
    ``tool_calls`` field. Accept that – but only for tools that were actually offered."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").removeprefix("json").strip()
    if not candidate.startswith(("{", "[")):
        return []
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    items = data if isinstance(data, list) else [data]
    calls = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("name") not in names:
            return []
        arguments = item.get("arguments", item.get("parameters", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return []
        if not isinstance(arguments, dict):
            return []
        calls.append(ToolCall(f"inline_{index}", item["name"], arguments))
    return calls


class OpenAICompatible:
    """OpenAI chat completions with tools – also Ollama (``/v1``), LM Studio, vLLM and others."""

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: float = 120, name="openai_compatible"):
        if not model:
            raise ProviderError("Bitte ein Modell angeben.")
        self.base_url, self.model, self.api_key = check_url(base_url), model, api_key
        self.timeout, self.name = timeout, name

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def _messages(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                calls = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    }
                    for c in m["tool_calls"]
                ]
                out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": calls})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn:
        payload: dict = {"model": self.model, "messages": self._messages(system, messages)}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.schema}}
                for t in tools
            ]
        data = _request(
            "POST", f"{self.base_url}/chat/completions", timeout=self.timeout, headers=self._headers(), payload=payload
        )
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Unerwartete Antwort des KI-Anbieters.") from exc
        calls = []
        for index, call in enumerate(message.get("tool_calls") or []):
            arguments = call.get("function", {}).get("arguments") or "{}"
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                parsed = {"_invalid_json": arguments}
            calls.append(ToolCall(call.get("id") or f"call_{index}", call["function"]["name"], parsed))
        text = message.get("content") or ""
        if not calls and (inline := _inline_tool_calls(text, {t.name for t in tools})):
            return Turn(text="", tool_calls=inline)
        return Turn(text=text, tool_calls=calls)

    def models(self) -> list[str]:
        data = _request("GET", f"{self.base_url}/models", timeout=min(self.timeout, 15), headers=self._headers())
        return sorted(m.get("id", "") for m in data.get("data", []))


class Anthropic:
    """Anthropic Messages API with tool use."""

    name = "anthropic"

    def __init__(self, base_url: str, model: str, api_key: str, timeout: float = 120):
        if not api_key:
            raise ProviderError("Für Anthropic ist ein API-Schlüssel nötig.")
        self.base_url, self.model, self.api_key, self.timeout = check_url(base_url), model, api_key, timeout

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION}

    @staticmethod
    def _messages(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "assistant":
                content = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                content += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                    for c in m.get("tool_calls") or []
                ]
                out.append({"role": "assistant", "content": content})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)  # results of parallel calls share one user turn
                else:
                    out.append({"role": "user", "content": [block]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn:
        payload = {
            "model": self.model,
            "max_tokens": 2048,
            "system": system,
            "messages": self._messages(messages),
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.schema} for t in tools],
        }
        data = _request(
            "POST", f"{self.base_url}/v1/messages", timeout=self.timeout, headers=self._headers(), payload=payload
        )
        text, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(block["id"], block["name"], block.get("input") or {}))
        return Turn(text="\n".join(text).strip(), tool_calls=calls)

    def models(self) -> list[str]:
        data = _request("GET", f"{self.base_url}/v1/models", timeout=15, headers=self._headers())
        return sorted(m.get("id", "") for m in data.get("data", []))


class Fake:
    """Deterministic provider for tests and evals: plays back scripted turns, records what it was sent."""

    name = "fake"

    def __init__(self, turns: list[Turn]):
        self.turns = list(turns)
        self.requests: list[dict] = []

    def complete(self, system: str, messages: list[dict], tools: list[ToolSpec]) -> Turn:
        self.requests.append(
            {"system": system, "messages": [dict(m) for m in messages], "tools": [t.name for t in tools]}
        )
        return self.turns.pop(0) if self.turns else Turn(text="(keine weitere Antwort)")

    def models(self) -> list[str]:
        return ["fake"]


def build(kind: str, *, base_url: str = "", model: str = "", api_key: str = "", timeout: float = 120):
    if kind in ("", "none"):
        return None
    url, model = base_url or DEFAULT_URLS.get(kind, ""), model or DEFAULT_MODELS.get(kind, "")
    if kind in ("ollama", "openai_compatible"):
        return OpenAICompatible(url, model, api_key, timeout, name=kind)
    if kind == "anthropic":
        return Anthropic(url, model, api_key, timeout)
    raise ProviderError(f"Unbekannter KI-Anbieter: {kind}")


def connection_test(provider) -> dict:
    """Models, tool support and latency – shown on the settings page."""
    started = time.monotonic()
    models = provider.models()
    probe = ToolSpec("ping", "Antwortet mit pong.", {"type": "object", "properties": {}, "additionalProperties": False})
    turn = provider.complete(
        "Rufe das Werkzeug ping auf.", [{"role": "user", "content": "Bitte ping aufrufen."}], [probe]
    )
    return {
        "models": models[:20],
        "tools": any(call.name == "ping" for call in turn.tool_calls),
        "latency_ms": int((time.monotonic() - started) * 1000),
    }
