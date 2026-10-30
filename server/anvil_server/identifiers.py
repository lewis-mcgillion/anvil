"""UUIDv7 identities shared by the API and workers."""

import secrets
import time
from uuid import UUID


def new_uuid7() -> UUID:
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    return UUID(int=(timestamp << 80) | (7 << 76) | (secrets.randbits(12) << 64)
                | (2 << 62) | secrets.randbits(62))
