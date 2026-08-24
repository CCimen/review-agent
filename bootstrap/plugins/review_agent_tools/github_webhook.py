"""Shared authentication for signed GitHub webhook-shaped requests."""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Compare one SHA-256 HMAC without leaking timing information."""
    if not signature.startswith("sha256="):
        return False
    expected = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)
