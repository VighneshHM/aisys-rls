"""Offline activation: a signed licence file, checked locally with the trusted vendor key. No network, no phone-home."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from shelfsense_lifecycle.bundle import LifecycleError, canonical, check_signature, key_id


def sign_payload_for(licence: dict) -> bytes:
    return canonical(licence)


def check_licence(path: Path, trusted_keys: list[bytes], today: date | None = None) -> dict:
    """Returns {status: valid|expired|invalid, customer, expires, problem}. Never raises on a bad file."""
    today = today or date.today()
    try:
        doc = json.loads(Path(path).read_text("utf-8"))
        licence, signature = doc["licence"], doc["signature"]
        if not isinstance(licence, dict) or not isinstance(signature, str):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "invalid", "problem": "The licence file is missing or not readable."}
    signer = next((key_id(k) for k in trusted_keys if check_signature(k, sign_payload_for(licence), signature)), None)
    if signer is None:
        return {"status": "invalid", "problem": "The licence signature is not valid for this installation."}
    if licence.get("product") != "shelfsense":
        return {"status": "invalid", "problem": "This licence is for a different product."}
    out = {
        "customer": str(licence.get("customer", "")),
        "expires": licence.get("expires"),
        "issued": licence.get("issued"),
    }
    expires = licence.get("expires")
    if expires:
        try:
            if date.fromisoformat(str(expires)) < today:
                return {**out, "status": "expired", "problem": f"The licence expired on {expires}."}
        except ValueError:
            return {**out, "status": "invalid", "problem": "The licence expiry date is not a valid date."}
    return {**out, "status": "valid", "problem": ""}


def require_valid(path: Path, trusted_keys: list[bytes]) -> dict:
    result = check_licence(path, trusted_keys)
    if result["status"] != "valid":
        raise LifecycleError("Activation failed: " + result["problem"])
    return result
