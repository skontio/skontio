"""Strip NUL bytes from query and form parameters.

PostgreSQL text fields cannot contain U+0000 – a stray "%00" in a search box or form field
would otherwise end in a 500 (a database DataError) instead of a normal page. No legitimate
input in Skontio contains it, so it is removed before any view sees the data.
"""

from __future__ import annotations

from django.http import QueryDict


def _clean(params: QueryDict) -> QueryDict:
    if not any("\x00" in value for values in params.lists() for value in values[1]) and not any(
        "\x00" in key for key in params
    ):
        return params
    cleaned = QueryDict(mutable=True, encoding=params.encoding)
    for key, values in params.lists():
        cleaned.setlist(key.replace("\x00", ""), [value.replace("\x00", "") for value in values])
    cleaned._mutable = False
    return cleaned


class NulByteMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.GET = _clean(request.GET)
        if request.method == "POST" and request.content_type in (
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        ):
            request.POST = _clean(request.POST)
        return self.get_response(request)
