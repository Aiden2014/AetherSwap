"""Process-local cooldowns for Steam market endpoints, without credential state."""

import math
import threading
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional
import requests


DEFAULT_RATE_LIMIT_SECONDS = 60.0
MAX_CONSECUTIVE_MARKET_429 = 3


class SteamMarketRateLimitExceeded(RuntimeError):
    """A buying task exhausted its bounded attempts to recover from HTTP 429."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(
            f"Steam 市场连续 {MAX_CONSECUTIVE_MARKET_429} 次 HTTP 429，自动重试已停止；"
            f"仍处于限流状态（冷却剩余约 {retry_after:.0f} 秒），请稍后手动重试"
        )


class SteamMarketCooldownActive(requests.RequestException):
    """A request was blocked locally before opening a Steam connection."""

    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Steam 市场 HTTP 429 冷却中，剩余约 {retry_after:.0f} 秒；本次未发送请求")


def parse_retry_after(value: object, *, now: Optional[float] = None) -> float:
    """Accept delta seconds or an HTTP date; unknown limits get a short cooldown."""
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = max(0.0, date.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError, OSError):
            return DEFAULT_RATE_LIMIT_SECONDS
    if not math.isfinite(delay) or delay < 0:
        return DEFAULT_RATE_LIMIT_SECONDS
    return max(1.0, delay)


class MarketCooldown:
    """Share an endpoint's 429 cooldown across tasks, without blocking a thread."""

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        self._clock = clock or time.monotonic
        self._until = 0.0
        self._failures = 0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - self._clock())

    def defer(self, retry_after: object = None) -> float:
        delay = parse_retry_after(retry_after)
        with self._lock:
            self._failures += 1
            if retry_after is None or not str(retry_after).strip():
                delay = min(900.0, DEFAULT_RATE_LIMIT_SECONDS * (2 ** min(self._failures - 1, 4)))
            now = self._clock()
            self._until = max(self._until, now + delay)
            return self._until - now

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0


# A limited market request must also stop fallback endpoints and history reads.
steam_market_cooldown = MarketCooldown()


def check_steam_market_cooldown() -> None:
    remaining = steam_market_cooldown.remaining()
    if remaining > 0:
        raise SteamMarketCooldownActive(remaining)
