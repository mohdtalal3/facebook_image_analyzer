"""Shared KIE account-wide rate limiter.

KIE allows ~20 requests/10s per account (KIE_MAX_REQUESTS_PER_WINDOW per
KIE_RATE_WINDOW_SECONDS). The pipeline runs as ONE job at a time (analysis
and image generation happen in sequence inside the same run_facebook.py
subprocess), so a thread-safe in-process limiter is enough — the
AI-generation threads all draw from the same process-wide window.

Product analysis no longer hits KIE (it runs through OpenAI — see
analysis.py); this limiter now only paces the KIE image-generation path
(generate.py uploads + task creation).
"""

import threading
import time

from constants import KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS


class RateLimiter:
    """Thread-safe in-process rate limiter."""

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.timestamps: list[float] = []

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.timestamps = [t for t in self.timestamps if now - t < self.period]
                if len(self.timestamps) < self.max_calls:
                    self.timestamps.append(now)
                    return
            time.sleep(0.2)


rate_limiter = RateLimiter(KIE_MAX_REQUESTS_PER_WINDOW, KIE_RATE_WINDOW_SECONDS)
