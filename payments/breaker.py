import math
import time
from typing import Callable, Optional

from django.conf import settings
from django.core.cache import cache

_clock: Optional[Callable[[], float]] = None


def set_clock(fn: Callable[[], float]) -> None:
    global _clock
    _clock = fn


def reset_clock() -> None:
    global _clock
    _clock = None


def now() -> float:
    return _clock() if _clock is not None else time.time()


def is_enabled() -> bool:
    return getattr(settings, "BREAKER_ENABLED", True)


def _key(provider: str, mode: str, suffix: str) -> str:
    return f"safepay:breaker:{provider}:{mode}:{suffix}"


def state(provider: str, livemode: bool) -> str:
    if not is_enabled():
        return "closed"

    mode = "live" if livemode else "test"
    open_until = cache.get(_key(provider, mode, "open_until"))
    if open_until is None:
        return "closed"

    if now() < open_until:
        return "open"
    return "half_open"


def allow_request(provider: str, livemode: bool) -> str:
    if not is_enabled():
        return "allow"

    st = state(provider, livemode)
    if st == "closed":
        return "allow"
    if st == "open":
        return "skip"

    mode = "live" if livemode else "test"
    probe_key = _key(provider, mode, "probe")
    probe_ttl = getattr(settings, "BREAKER_PROBE_TTL_SECONDS", 30)

    probe_expiry = cache.get(probe_key)
    if probe_expiry is not None:
        if now() < probe_expiry:
            return "skip"
        cache.delete(probe_key)

    if cache.add(probe_key, now() + probe_ttl, timeout=probe_ttl):
        return "probe"
    return "skip"


def record_failure(provider: str, livemode: bool) -> None:
    if not is_enabled():
        return

    mode = "live" if livemode else "test"
    current_state = state(provider, livemode)

    if current_state == "half_open":
        cooldown_key = _key(provider, mode, "cooldown")
        open_until_key = _key(provider, mode, "open_until")
        probe_key = _key(provider, mode, "probe")

        cooldown = cache.get(cooldown_key) or getattr(settings, "BREAKER_COOLDOWN_SECONDS", 60)
        max_cooldown = getattr(settings, "BREAKER_MAX_COOLDOWN_SECONDS", 600)
        new_cooldown = min(cooldown * 2, max_cooldown)

        cache.set(cooldown_key, new_cooldown, timeout=86400)
        cache.set(open_until_key, now() + new_cooldown, timeout=86400)
        cache.delete(probe_key)
        return

    failures_key = _key(provider, mode, "failures")
    window_start_key = _key(provider, mode, "window_start")
    window = getattr(settings, "BREAKER_WINDOW_SECONDS", 60)
    threshold = getattr(settings, "BREAKER_FAILURE_THRESHOLD", 5)

    current_time = now()
    window_start = cache.get(window_start_key)

    if window_start is None or (current_time - window_start >= window):
        cache.set(window_start_key, current_time, timeout=window)
        cache.set(failures_key, 1, timeout=window)
        count = 1
    else:
        cache.add(failures_key, 0, timeout=window)
        count = cache.incr(failures_key)

    if count >= threshold:
        cooldown = getattr(settings, "BREAKER_COOLDOWN_SECONDS", 60)
        cooldown_key = _key(provider, mode, "cooldown")
        open_until_key = _key(provider, mode, "open_until")

        cache.set(cooldown_key, cooldown, timeout=86400)
        cache.set(open_until_key, current_time + cooldown, timeout=86400)
        cache.delete(failures_key)
        cache.delete(window_start_key)

        from .alerts import send_alert_message
        send_alert_message(
            f"Provider {provider} ({mode}) circuit OPEN after {threshold} failures in {window}s. "
            "New payments will use the backup until it recovers."
        )


def record_success(provider: str, livemode: bool) -> None:
    if not is_enabled():
        return

    mode = "live" if livemode else "test"
    open_until_key = _key(provider, mode, "open_until")
    cooldown_key = _key(provider, mode, "cooldown")
    probe_key = _key(provider, mode, "probe")
    failures_key = _key(provider, mode, "failures")
    window_start_key = _key(provider, mode, "window_start")

    was_open_or_half_open = cache.get(open_until_key) is not None

    cache.delete(open_until_key)
    cache.delete(cooldown_key)
    cache.delete(probe_key)
    cache.delete(failures_key)
    cache.delete(window_start_key)

    if was_open_or_half_open:
        from .alerts import send_alert_message
        send_alert_message(f"Provider {provider} ({mode}) circuit CLOSED.")


def retry_after(provider: str, livemode: bool) -> int:
    if not is_enabled():
        return 1

    mode = "live" if livemode else "test"
    open_until = cache.get(_key(provider, mode, "open_until"))
    if open_until is not None:
        remaining = open_until - now()
        if remaining > 0:
            return max(1, int(math.ceil(remaining)))

    probe_expiry = cache.get(_key(provider, mode, "probe"))
    if probe_expiry is not None:
        rem_probe = probe_expiry - now()
        if rem_probe > 0:
            return max(1, int(math.ceil(rem_probe)))

    return 1
