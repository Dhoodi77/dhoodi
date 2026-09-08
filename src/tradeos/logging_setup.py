"""Structured JSON logging with correlation IDs.

Every log line is a single JSON object. A correlation id (usually an
opportunity id) can be bound to the current context so one opportunity can be
traced through the whole agent pipeline. A redaction filter scrubs anything
that looks like a secret before it reaches any handler.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

# Patterns that must never appear in logs: API keys, hex private keys,
# base58 secret-looking blobs, bearer tokens.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])"),
    re.compile(r"[1-9A-HJ-NP-Za-km-z]{85,90}"),  # base58 solana keypair length
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{16,}"),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def set_correlation_id(cid: str | None) -> None:
    _correlation_id.set(cid)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def new_correlation_id(prefix: str = "opp") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        cid = _correlation_id.get()
        if cid:
            entry["cid"] = cid
        extra = getattr(record, "extra_fields", None)
        if extra:
            entry.update({k: redact(v) if isinstance(v, str) else v for k, v in extra.items()})
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactionFilter())
    root.addHandler(handler)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields) -> None:
    logger.log(level, msg, extra={"extra_fields": fields})
