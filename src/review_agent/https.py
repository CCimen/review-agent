"""Redirect handling that keeps a credentialed request on one HTTPS origin."""

from __future__ import annotations

from http.client import HTTPMessage
import urllib.parse
import urllib.request
from typing import IO


def credentialed_https_origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("redirect URL must use one HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("redirect URL must use one HTTPS origin")
    return parsed.scheme, parsed.hostname.lower(), port


def https_api_origin(url: str) -> tuple[str, str, int]:
    try:
        origin = credentialed_https_origin(url)
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise ValueError("api_url must be an HTTPS API root") from exc
    if parsed.query or parsed.fragment:
        raise ValueError("api_url must be an HTTPS API root")
    return origin


class SameOriginHttpsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow credentialed redirects only within one HTTPS origin."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            same_origin = credentialed_https_origin(
                req.full_url
            ) == credentialed_https_origin(newurl)
        except ValueError:
            same_origin = False
        if not same_origin:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)
