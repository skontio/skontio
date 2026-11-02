from django.urls import include, path
from oauth2_provider import urls as oauth2_urls
from oauth2_provider import views as oauth2_views

from skontio.accounts import views as accounts
from skontio.attachments import views as attachment_views
from skontio.mcp import views as mcp_views
from skontio.mcp.auth import mcp_endpoint
from skontio.web import accounting_views as accounting
from skontio.web import assistant_views, transfer_views
from skontio.web import numbering_views as numbering
from skontio.web import procedure_views as procedure
from skontio.web import security_views as security
from skontio.web import taxstatus_views as taxstatus
from skontio.web import template_views as layouts

urlpatterns = [
    # /healthz is answered by skontio.core.health.HealthCheckMiddleware
    # No Django admin: its password login would bypass the LAN limit, lockout and second factor of
    # /auth/local/. Everything is managed in the app (Einstellungen).
    # RFC 8414 / RFC 9728 documents must live at the origin root
    path("", include((oauth2_urls.metadata_urlpatterns, "oauth2_metadata"))),
    # own consent screen (admins only, German, privacy notes) in front of the toolkit's view
    path("oauth/authorize/", mcp_views.ConsentView.as_view(), name="oauth_authorize"),
    # only what MCP clients need – no device flow, introspection or application management on the internet
    path(
        "oauth/",
        include(
            (
                [
                    path("authorize/", oauth2_views.AuthorizationView.as_view(), name="authorize"),
                    path("token/", oauth2_views.TokenView.as_view(), name="token"),
                    path("revoke_token/", oauth2_views.RevokeTokenView.as_view(), name="revoke-token"),
                ],
                "oauth2_provider",
            )
        ),
    ),  # fmt: skip
    path("einstellungen/ki-zugriff/", mcp_views.ai_access, name="ai_access"),
    path("einstellungen/ki-zugriff/<int:pk>/widerrufen/", mcp_views.ai_access_revoke, name="ai_access_revoke"),
    path("einstellungen/ki-zugriff/tokens/", mcp_views.ai_token_create, name="ai_token_create"),
    path("einstellungen/ki-zugriff/tokens/<int:pk>/widerrufen/", mcp_views.ai_token_revoke, name="ai_token_revoke"),
    path("einstellungen/ki-zugriff/skontio-mcp.py", mcp_views.bridge_download, name="mcp_bridge_download"),
    path("mcp", mcp_endpoint, name="mcp"),
    path("auth/login", accounts.oidc_login, name="login"),
    path("auth/callback", accounts.oidc_callback, name="oidc_callback"),
    path("auth/logout", accounts.oidc_logout, name="logout"),
    path("auth/local/", accounts.LocalLoginView.as_view(), name="local_login"),
    path("auth/local/2fa/", accounts.local_second_factor, name="local_login_2fa"),
    path("dateien/hochladen/", attachment_views.upload, name="attachment_upload"),
    path("dateien/<int:pk>/", attachment_views.download, name="attachment_download"),
    path("dateien/<int:pk>/loeschen/", attachment_views.remove, name="attachment_remove"),
    path("einstellungen/vorlagen/", layouts.template_list, name="template_list"),
    path("einstellungen/vorlagen/neu/", layouts.template_create, name="template_create"),
    path("einstellungen/vorlagen/anhaenge/", layouts.standard_file_upload, name="standard_file_upload"),
    path("einstellungen/vorlagen/<int:pk>/", layouts.template_edit, name="template_edit"),
    path("einstellungen/vorlagen/<int:pk>/loeschen/", layouts.template_delete, name="template_delete"),
    path("einstellungen/textbausteine/", layouts.text_blocks, name="text_blocks"),
    path("einstellungen/nummernkreise/", numbering.numbering, name="numbering"),
    path("einstellungen/buchhaltung/", accounting.ledger, name="ledger"),
    path("einstellungen/gesamtexport/", transfer_views.full_export, name="full_export"),
    path("auswertungen/datev/", accounting.datev_export, name="datev_export"),
    path("einstellungen/steuerstatus/", taxstatus.tax_status, name="tax_status"),
    path("einstellungen/steuerstatus/wechsel/", taxstatus.tax_status_change, name="tax_status_change"),
    path("einstellungen/steuerstatus/korrektur/<int:pk>/", taxstatus.invoice_correct, name="invoice_correct"),
    path("dokumente/<int:pk>/zahlungen-uebernehmen/", taxstatus.payments_transfer, name="payments_transfer"),
    path("einstellungen/verfahrensdokumentation/", procedure.procedure, name="procedure"),
    path("einstellungen/verfahrensdokumentation/neu/", procedure.procedure_generate, name="procedure_generate"),
    path("einstellungen/verfahrensdokumentation/<int:pk>/", procedure.procedure_download, name="procedure_download"),
    path("einstellungen/assistent/", assistant_views.assistant_settings, name="assistant_settings"),
    path("einstellungen/assistent/test/", assistant_views.assistant_test, name="assistant_test"),
    path("einstellungen/sicherheit/", security.security, name="security"),
    path("einstellungen/sicherheit/einrichten/", security.security_start, name="security_start"),
    path("einstellungen/sicherheit/bestaetigen/", security.security_confirm, name="security_confirm"),
    path("einstellungen/sicherheit/aus/", security.security_disable, name="security_disable"),
    path("", include("skontio.web.urls")),
]
