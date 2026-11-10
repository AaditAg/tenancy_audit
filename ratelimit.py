# ratelimit.py
# ----------------------------------------------------------------------
# Tiny in-memory sliding-window limiter used to protect a server-side API
# key when the app runs in "managed" mode (users never enter a key).
# State lives in the process, so it resets on restart and is per-instance;
# good enough to stop casual abuse, not a substitute for provider-side caps.
# ----------------------------------------------------------------------

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional


class RateLimiter:
    def __init__(self, per_client: int = 3, global_limit: int = 100, window_s: int = 3600,
                 clock=time.monotonic):
        self.per_client = per_client
        self.global_limit = global_limit
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._client: Dict[str, Deque[float]] = defaultdict(deque)
        self._global: Deque[float] = deque()

    def _prune(self, q: Deque[float], now: float) -> None:
        while q and now - q[0] >= self.window_s:
            q.popleft()

    def allow(self, client_id: str) -> bool:
        """Record and approve one AI audit, or refuse it if a limit is hit. A limit of 0 means unlimited."""
        with self._lock:
            now = self._clock()
            cq = self._client[client_id]
            self._prune(cq, now)
            self._prune(self._global, now)
            if self.per_client and len(cq) >= self.per_client:
                return False
            if self.global_limit and len(self._global) >= self.global_limit:
                return False
            cq.append(now)
            self._global.append(now)
            return True

    def remaining(self, client_id: str) -> Optional[int]:
        with self._lock:
            now = self._clock()
            cq = self._client[client_id]
            self._prune(cq, now)
            return None if not self.per_client else max(self.per_client - len(cq), 0)
