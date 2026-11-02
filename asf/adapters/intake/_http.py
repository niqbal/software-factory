"""Tiny HTTP + signature helpers shared by intake adapters (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request
from typing import Any


def http_json(method: str, url: str, headers: dict[str, str] | None = None,
              body: Any = None, timeout: float = 30) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs come from config
        raw = resp.read()
    return json.loads(raw) if raw else None


def hmac_hex(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def safe_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}
