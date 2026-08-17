from __future__ import annotations

"""
Keeps the last N log lines in memory so the admin failure notification can
include a tail of the log. Nothing is written to disk.
"""

import logging
from collections import deque

_BUFFER: deque[str] = deque(maxlen=200)


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _BUFFER.append(self.format(record))
        except Exception:
            pass


def install_ring_buffer() -> None:
    """Attaches the ring buffer handler to the root logger (once)."""
    root = logging.getLogger()
    if any(isinstance(h, RingBufferHandler) for h in root.handlers):
        return
    handler = RingBufferHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    handler.setLevel(logging.INFO)
    root.addHandler(handler)


def last_lines(count: int = 20) -> list[str]:
    n = max(1, min(count, _BUFFER.maxlen or count))
    return list(_BUFFER)[-n:]
