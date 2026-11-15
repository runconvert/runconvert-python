"""Webhook signature checks.

Every delivery has an ``X-RunConvert-Signature`` header: ``sha256=`` followed by the hex
HMAC-SHA256 of the **raw** request body, keyed with the endpoint's secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Optional, Union

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def verify_signature(raw_body: Union[str, bytes], signature: Optional[str], secret: str) -> bool:
    """True when ``signature`` matches the body. Pass the body exactly as received."""
    if not signature or not secret:
        return False
    given = signature.strip()
    if given.lower().startswith("sha256="):
        given = given[7:]
    given = given.lower()
    if not _HEX64.match(given):
        return False
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def construct_event(raw_body: Union[str, bytes], signature: Optional[str], secret: str) -> dict:
    """Verifies the signature, then parses the event. Raises ``ValueError`` if it doesn't match."""
    if not verify_signature(raw_body, signature, secret):
        raise ValueError("Invalid RunConvert webhook signature")
    return json.loads(raw_body)
