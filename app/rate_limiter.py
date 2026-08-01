"""Simple token-bucket limiter. Capacity = burst size, refill = rate/ms.
Sustained `rate_per_minute` submissions/minute succeed indefinitely."""

import time


class TokenBucket:
    def __init__(self, rate_per_minute: float, burst: float):
        self.rate_per_sec = rate_per_minute / 60.0
        self.capacity = burst
        self.tokens = burst
        self.last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed <= 0:
            return
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
        self.last_refill = now

    def try_consume(self):
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return {"allowed": True}
        deficit = 1 - self.tokens
        seconds_until_token = deficit / self.rate_per_sec
        return {"allowed": False, "retry_after_seconds": max(1, int(seconds_until_token) + 1)}
