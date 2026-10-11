"""Django settings for Skontio.

Every value can be overridden with a ``SKONTIO_*`` environment variable so the
same image runs unchanged on a laptop, in CI and on TrueNAS.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SKONTIO_DATA_DIR", BASE_DIR.parent.parent / "var" / "data"))


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def read_secret(name: str, default: str | None = None) -> str | None:
    """Read ``NAME`` or the file referenced by ``NAME_FILE`` (Docker secrets)."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    return os.environ.get(name, default)


def database_from_url(url: str) -> dict:
    """Translate ``sqlite:///path`` or ``postgres://user:pw@host:port/db`` into Django settings."""
    parsed = urlparse(url)
    if parsed.scheme == "sqlite":
        path = unquote(parsed.path)
        if url.startswith("sqlite:////"):
            name = path
        else:
            name = str(DATA_DIR / path.lstrip("/")) if path.strip("/") else str(DATA_DIR / "skontio.sqlite3")
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": name,
            "OPTIONS": {
                # BEGIN IMMEDIATE serialises writers; required for gap-free numbering.
                "transaction_mode": "IMMEDIATE",
                "timeout": 30,
                "init_command": (
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA foreign_keys=ON;"
                    "PRAGMA busy_timeout=30000;"
                ),
            },
            # File-based test DB: in-memory shared cache uses table locks that ignore busy_timeout.
            "TEST": {
                "NAME": os.environ.get("SKONTIO_TEST_SQLITE_NAME")
                or str(Path(tempfile.gettempdir()) / f"skontio-test-{os.getpid()}.sqlite3")
            },
        }
    if parsed.scheme in {"postgres", "postgresql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "CONN_MAX_AGE": 60,
        }
    raise ValueError(f"Unsupported database URL scheme: {parsed.scheme!r}")


DEBUG = env_bool("SKONTIO_DEBUG", False)
_DEV_OR_TEST = DEBUG or bool(os.environ.get("PYTEST_VERSION"))
SECRET_KEY = read_secret("SKONTIO_SECRET_KEY") or ("insecure-dev-key" if _DEV_OR_TEST else None)
if not SECRET_KEY:
    raise RuntimeError("SKONTIO_SECRET_KEY (or SKONTIO_SECRET_KEY_FILE) must be set")

PUBLIC_URL = os.environ.get("SKONTIO_PUBLIC_URL", "http://localhost:8000").rstrip("/")
ALLOWED_HOSTS = env_list("SKONTIO_ALLOWED_HOSTS", urlparse(PUBLIC_URL).hostname or "localhost")
CSRF_TRUSTED_ORIGINS = [PUBLIC_URL]
TRUSTED_PROXIES = env_list("SKONTIO_TRUSTED_PROXIES")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "oauth2_provider",
    "skontio.core",
    "skontio.numbering",
    "skontio.documents",
    "skontio.taxstatus",
    "skontio.catalog",
    "skontio.crm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "skontio.urls"
ASGI_APPLICATION = "skontio.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": database_from_url(os.environ.get("SKONTIO_DATABASE_URL", "sqlite:///skontio.sqlite3"))}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "de-de"
TIME_ZONE = os.environ.get("SKONTIO_TIME_ZONE", "Europe/Berlin")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(os.environ.get("SKONTIO_STATIC_ROOT", BASE_DIR.parent.parent / "var" / "static"))

SESSION_COOKIE_SECURE = PUBLIC_URL.startswith("https://")
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# Skontio as OAuth 2.1 authorization server for /mcp (MCP authorization spec 2026-07-28).
MCP_SCOPES = {
    "catalog:read": "Leistungskatalog lesen",
    "catalog:write": "Leistungen anlegen und ändern",
    "customers:read": "Kunden suchen (gefilterte Felder)",
    "customers:write": "Kunden anlegen",
    "drafts:read": "Entwürfe prüfen und als Vorschau ansehen",
    "drafts:write": "Angebots- und Rechnungsentwürfe anlegen und ändern",
    "documents:read": "Rechnungen und offene Posten lesen",
    "reports:read": "Auswertungen lesen",
}
OAUTH2_PROVIDER = {
    "SCOPES": MCP_SCOPES,
    "DEFAULT_SCOPES": ["catalog:read"],
    "PKCE_REQUIRED": True,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 900,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 30 * 24 * 3600,
    "ROTATE_REFRESH_TOKEN": True,
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http"],  # http only for loopback clients (RFC 8252)
    "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,  # store token hashes only
    "OIDC_ISS_ENDPOINT": PUBLIC_URL,
    "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": f"{PUBLIC_URL}/mcp",
    "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": [PUBLIC_URL],
    "OAUTH2_PROTECTED_RESOURCE_NAME": "Skontio MCP",
    "RESOURCE_SERVER_TOKEN_RESOURCE_VALIDATOR": "skontio.mcp.auth.resource_matches_exactly",
}

OIDC_ISSUER = os.environ.get("SKONTIO_OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.environ.get("SKONTIO_OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = read_secret("SKONTIO_OIDC_CLIENT_SECRET", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("SKONTIO_LOG_LEVEL", "INFO")},
}
