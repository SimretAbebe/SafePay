"""Pure-function utilities for benchmark statistics."""

import math


def percentile(values, p):
    """Return the p-th percentile using the nearest-rank method.

    Accepts unsorted input (sorts internally without modifying the input).
    Returns None for an empty list.
    """
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    idx = max(0, min(math.ceil(p / 100 * n) - 1, n - 1))
    return vals[idx]


def summarize(durations_ms, errors, throttled, wall_seconds):
    """Summarize a list of durations into common benchmark metrics."""
    n = len(durations_ms)
    if not n:
        return {
            "count": 0,
            "mean": None,
            "min": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
            "requests_per_second": 0,
            "errors": errors,
            "throttled": throttled,
        }

    return {
        "count": n,
        "mean": sum(durations_ms) / n,
        "min": min(durations_ms),
        "p50": percentile(durations_ms, 50),
        "p95": percentile(durations_ms, 95),
        "p99": percentile(durations_ms, 99),
        "max": max(durations_ms),
        "requests_per_second": n / wall_seconds if wall_seconds else 0,
        "errors": errors,
        "throttled": throttled,
    }


def histogram(durations_ms, buckets=10):
    """Divide durations into equal-width buckets: [(low, high, count), ...]."""
    if not durations_ms:
        return []

    buckets = max(1, buckets)
    lo, hi = min(durations_ms), max(durations_ms)
    if lo == hi:
        return [(lo, hi, len(durations_ms))]

    width = (hi - lo) / buckets
    result = []
    for b in range(buckets):
        b_lo = lo + b * width
        b_hi = lo + (b + 1) * width
        if b == buckets - 1:
            count = sum(1 for v in durations_ms if b_lo <= v <= b_hi)
        else:
            count = sum(1 for v in durations_ms if b_lo <= v < b_hi)
        result.append((b_lo, b_hi, count))
    return result


def render_histogram(buckets):
    """Render histogram buckets as an ASCII bar chart (max 40 chars wide)."""
    if not buckets:
        return ""

    max_count = max(c for _, _, c in buckets) or 1
    lines = []
    for lo, hi, count in buckets:
        bar_len = int(count / max_count * 40)
        bar = "#" * bar_len
        lines.append(f"{lo:8.1f} - {hi:8.1f} ms | {bar} {count}")
    return "\n".join(lines) + "\n"
