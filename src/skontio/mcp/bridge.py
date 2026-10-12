#!/usr/bin/env python3
"""skontio-mcp – stdio bridge from a local MCP client (Claude Desktop, …) to Skontio's /mcp endpoint.

Standard library only, so it also runs as a single downloaded file (``python3 skontio-mcp.py``).

    SKONTIO_URL=https://skontio.example.de SKONTIO_TOKEN=skt_… skontio-mcp

Reads one JSON-RPC message per line from stdin, POSTs it to ``<SKONTIO_URL>/mcp`` with the token and
the MCP headers the server checks (protocol version, method, tool name), writes the answer to stdout.
Diagnostics go to stderr only. The token is read from the environment, never from the command line
(it would be visible in the process list).
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

META_VERSION = "io.modelcontextprotocol/protocolVersion"
INTERNAL_ERROR = -32603
TIMEOUT_SECONDS = 60


def _log(message: str) -> None:
    print(f"skontio-mcp: {message}", file=sys.stderr, flush=True)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint(base_url: str, *, allow_http: bool = False) -> str:
    """``https://host[/prefix]`` → ``https://host[/prefix]/mcp``. Plain http only for loopback or on request."""
    parsed = urllib.parse.urlsplit(base_url.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"SKONTIO_URL muss mit https:// beginnen: {base_url!r}")
    if parsed.scheme == "http" and not (allow_http or _is_loopback(parsed.hostname)):
        raise ValueError("Unverschlüsseltes http nur für localhost – sonst https verwenden (oder --allow-http)")
    path = parsed.path.rstrip("/")
    if not path.endswith("/mcp"):
        path += "/mcp"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _header_value(value: str) -> str:
    """Non-ASCII tool names travel base64-encoded (MCP spec: header value encoding)."""
    if value.isascii():
        return value
    return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="


class Bridge:
    def __init__(self, url: str, token: str, *, opener=None):
        self.url = url
        self.token = token
        self.version: str | None = None  # negotiated by initialize (legacy clients)
        self.open = opener or urllib.request.urlopen

    def headers_for(self, message: dict) -> dict:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "skontio-mcp",
        }
        if version := meta.get(META_VERSION) or self.version:
            headers["MCP-Protocol-Version"] = version
        headers["Mcp-Method"] = message["method"]
        if message["method"] == "tools/call" and isinstance(params.get("name"), str):
            headers["Mcp-Name"] = _header_value(params["name"])
        return headers

    def forward(self, line: str) -> str | None:
        """Return the line to write to stdout, or ``None`` (notifications, client responses)."""
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return None  # Skontio never sends requests, so there is nothing to answer
        id_ = message.get("id")
        request = urllib.request.Request(  # noqa: S310 – scheme checked in endpoint()
            self.url, data=line.encode("utf-8"), headers=self.headers_for(message), method="POST"
        )
        try:
            with self.open(request, timeout=TIMEOUT_SECONDS) as response:
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            _log(f"Skontio nicht erreichbar: {exc}")
            return self._error(id_, f"Skontio ist nicht erreichbar ({exc}).")
        if id_ is None:
            return None
        try:
            reply = json.loads(body) if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            reply = None
        if not (isinstance(reply, dict) and reply.get("jsonrpc") == "2.0"):
            return self._error(id_, self._explain(status, reply))
        if message["method"] == "initialize" and isinstance(reply.get("result"), dict):
            self.version = reply["result"].get("protocolVersion")
        return json.dumps(reply, ensure_ascii=False)

    @staticmethod
    def _explain(status: int, reply) -> str:
        if status == 401:
            return "Skontio hat den Zugangstoken abgelehnt (abgelaufen, widerrufen oder falsch)."
        if status == 403 and isinstance(reply, dict) and reply.get("error") == "insufficient_scope":
            return f"Dem Zugangstoken fehlt die Berechtigung {reply.get('scope')}."
        if status == 429:
            return "Zu viele Anfragen an Skontio – bitte eine Minute warten."
        if status == 503:
            return "Skontio ist noch nicht eingerichtet."
        return f"Unerwartete Antwort von Skontio (HTTP {status})."

    @staticmethod
    def _error(id_, text: str) -> str | None:
        if id_ is None:
            return None
        return json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": INTERNAL_ERROR, "message": text}})

    def run(self, stdin=None, stdout=None) -> None:
        stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            if (out := self.forward(line.strip())) is not None:
                stdout.write(out + "\n")
                stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="skontio-mcp", description="stdio-Bridge zu Skontio (MCP)")
    parser.add_argument("--url", default=os.environ.get("SKONTIO_URL", ""), help="Skontio-Adresse (SKONTIO_URL)")
    parser.add_argument("--allow-http", action="store_true", help="http auch außerhalb von localhost erlauben")
    args = parser.parse_args(argv)
    token = os.environ.get("SKONTIO_TOKEN", "").strip()
    if not token.startswith("skt_"):
        _log("SKONTIO_TOKEN fehlt – Token unter Einstellungen → KI-Zugriff anlegen.")
        return 2
    try:
        url = endpoint(args.url, allow_http=args.allow_http)
    except ValueError as exc:
        _log(str(exc))
        return 2
    _log(f"leite MCP-Nachrichten weiter an {url}")
    try:
        Bridge(url, token).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
