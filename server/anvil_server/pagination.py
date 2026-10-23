"""Authenticated and query-bound pagination cursors."""

import base64
import hashlib
import hmac
import json


class CursorCodec:
    def __init__(self, secret: bytes):
        if len(secret) < 16:
            raise ValueError("Cursor secret must be at least 16 bytes")
        self.secret = secret

    def encode(self, resource: str, scope: str, key: str, filters: dict[str, str]) -> str:
        if not resource or not scope or not key:
            raise ValueError("Incomplete cursor")
        data = json.dumps({"version": 1, "resource": resource, "scope": scope,
                           "key": key, "filters": filters}, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self.secret, data, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature + data).decode().rstrip("=")

    def decode(self, cursor: str, resource: str, scope: str, filters: dict[str, str]) -> str:
        if len(cursor) > 4096:
            raise ValueError("Oversized cursor")
        try:
            raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
            signature, data = raw[:32], raw[32:]
            expected = hmac.new(self.secret, data, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid signature")
            decoded = json.loads(data)
            if decoded["version"] != 1 or decoded["resource"] != resource or decoded["scope"] != scope or decoded["filters"] != filters:
                raise ValueError("Cursor query mismatch")
            return decoded["key"]
        except (KeyError, TypeError, UnicodeError, ValueError) as error:
            raise ValueError("Invalid or mismatched cursor") from error
