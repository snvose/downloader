from __future__ import annotations

"""
bot/log_buffer.py — Son N log satırını bellekte tutan handler.

Admin hata bildiriminde "son 20 satır log" gönderebilmek için kullanılır.
Diske yazmaz; yalnızca bir collections.deque ring buffer tutar.
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
    """Root logger'a ring buffer handler ekler (bir kez)."""
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
