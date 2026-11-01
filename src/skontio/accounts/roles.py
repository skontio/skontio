"""Roles beyond Django's staff flag.

"Nur Entwürfe": may create customers, items, expenses and drafts, but never finalize, send,
cancel or book money. Assigned from the OIDC group SKONTIO_OIDC_DRAFTS_GROUP or locally via the admin.
Administrators are never restricted.
"""

from __future__ import annotations

DRAFTS_ONLY_GROUP = "Nur Entwürfe"


def drafts_only(user) -> bool:
    if not user.is_authenticated or user.is_staff:
        return False
    cached = getattr(user, "_skontio_drafts_only", None)
    if cached is None:
        cached = user.groups.filter(name=DRAFTS_ONLY_GROUP).exists()
        user._skontio_drafts_only = cached
    return cached
