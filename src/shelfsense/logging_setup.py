"""Structured (JSON) logging with masking of sensitive values."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

_MASKS = [
    (re.compile(r"(?i)(password|secret|token|authorization)(\"?\s*[:=]\s*\"?)[^\s\",}]+"), r"\1\2***"),
    (re.compile(r"\b(\d{4})\d{4,}(\d{2})\b"), r"\1****\2"),  # long digit runs (cards, phones)
]


def mask(text: str) -> str:
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": mask(record.getMessage()),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            payload["ctx"] = json.loads(mask(json.dumps(extra, default=str)))
        if record.exc_info:
            payload["exc"] = mask(self.formatException(record.exc_info))
        return json.dumps(payload)


class MaskingTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return mask(super().format(record))


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if fmt == "json" else MaskingTextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
