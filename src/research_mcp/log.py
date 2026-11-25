"""One JSON line per event on stdout. Callers pass only safe fields: never a token,
a question or a report (REQ-12). `user` is a hash of the Cognito sub."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid


def user_hash(sub: str) -> str:
    return hashlib.sha256(f"research-mcp/{sub}".encode()).hexdigest()[:16]


def ref_id() -> str:
    """Short reference for an error, shown to the user and in the log."""
    return uuid.uuid4().hex[:8]


def log(msg: str, **fields: object) -> None:
    rec = {"ts": round(time.time(), 3), "msg": msg}
    rec.update({k: v for k, v in fields.items() if v is not None})
    sys.stdout.write(json.dumps(rec, default=str) + "\n")
    sys.stdout.flush()
