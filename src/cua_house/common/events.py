"""Small JSONL event logging helpers shared across AgentHLE."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(slots=True)
class JsonlEventLogger:
    """Append structured events to a JSONL file."""

    path: Path
    component: str
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event: str, **fields: Any) -> None:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "component": self.component,
            "event": event,
            **{key: _jsonable(value) for key, value in fields.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
