"""Thread-safe port allocation for env-server."""

from __future__ import annotations

from threading import Lock


class PortPool:
    """Simple in-memory port allocator."""

    def __init__(self, start: int, end: int):
        if end < start:
            raise ValueError("end must be >= start")
        self._start = start
        self._end = end
        self._lock = Lock()
        self._allocated: set[int] = set()

    def allocate(self) -> int:
        with self._lock:
            for port in range(self._start, self._end + 1):
                if port not in self._allocated:
                    self._allocated.add(port)
                    return port
        raise RuntimeError(f"no free ports available in range {self._start}-{self._end}")

    def release(self, port: int) -> None:
        with self._lock:
            self._allocated.discard(port)

    @property
    def available_count(self) -> int:
        with self._lock:
            return (self._end - self._start + 1) - len(self._allocated)
