"""Management command that benchmarks SafePay API latency."""

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from urllib.parse import urlparse

import requests

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from payments.bench_stats import histogram, render_histogram, summarize
from payments.models import Merchant, Payment

CAVEAT = (
    "Numbers are from this machine, the dev server, a local database "
    "and a fake provider; they show relative behavior, not production capacity."
)

ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
SCENARIOS = ("all", "list", "detail", "detail_304", "checkout", "replay")


def parse_server_timing(header):
    """Parse Server-Timing header into {metric: duration_ms}."""
    timing = {}
    if not header:
        return timing
    for part in header.split(","):
        match = re.search(r"(\w+);dur=([\d.]+)", part.strip())
        if match:
            timing[match.group(1)] = float(match.group(2))
    return timing


class Command(BaseCommand):
    help = "Benchmark SafePay API latency and report percentiles."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Dev server base URL.")
        parser.add_argument("--requests", type=int, default=300, help="Requests per scenario (max 5000).")
        parser.add_argument("--concurrency", type=int, default=8, help="Worker threads (max 32).")
        parser.add_argument("--warmup", type=int, default=20, help="Warmup requests excluded from stats.")
        parser.add_argument("--scenario", default="all", choices=SCENARIOS, help="Scenario to benchmark.")
        parser.add_argument("--json", dest="json_path", default=None, help="Save summary to JSON file.")
        parser.add_argument("--budget-p95-ms", type=float, default=None, help="Target p95 latency budget in ms.")
        parser.add_argument("--i-know-this-is-not-production", action="store_true", help="Allow when DEBUG=False.")

    def _check_safety(self, base_url, options):
        host = urlparse(base_url).hostname or ""
        if host not in ALLOWED_HOSTS:
            raise CommandError(f"Refusing non-local host '{host}'. Allowed: {', '.join(sorted(ALLOWED_HOSTS))}.")

        if not getattr(settings, "DEBUG", False) and not options["i_know_this_is_not_production"]:
            raise CommandError("DEBUG is False. Pass --i-know-this-is-not-production to proceed.")

        if getattr(settings, "LIVE_MODE_AVAILABLE", False):
            raise CommandError("LIVE_MODE_AVAILABLE is True. Start server with LIVE_MODE_AVAILABLE=False.")

        if options["requests"] > 5000:
            self.stderr.write(self.style.WARNING(f"Capping --requests from {options['requests']} to 5000."))
            options["requests"] = 5000

        if options["concurrency"] > 32:
            self.stderr.write(self.style.WARNING(f"Capping --concurrency from {options['concurrency']} to 32."))
            options["concurrency"] = 32

    def _run_scenario(self, task_fn, total_requests, concurrency, sessions):
        durations = []
        timing_samples = {}
        errors = 0
        throttled = 0
        all_429 = True

        wall_start = time.perf_counter()

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(task_fn, sessions[i % len(sessions)], i)
                for i in range(total_requests)
            ]

            for future in as_completed(futures):
                try:
                    elapsed, code, timing = future.result()
                except CommandError:
                    raise
                except Exception:
                    errors += 1
                    continue

                if code != 429:
                    all_429 = False

                if code == 429:
                    throttled += 1
                elif code in (200, 201, 304):
                    durations.append(elapsed)
                    for k, v in timing.items():
                        timing_samples.setdefault(k, []).append(v)
                else:
                    errors += 1

                if all_429 and (throttled >= 10 or throttled >= total_requests):
                    break

        wall_time = time.perf_counter() - wall_start
        return durations, errors, throttled, wall_time, timing_samples, all_429

    def _make_task(self, name, base_url, payment_id, etag, replay_key=None):
        if name == "list":
            url = f"{base_url}/api/payments/"
            return lambda s, i: self._timed_request(s, "GET", url)

        if name == "detail":
            url = f"{base_url}/api/payments/{payment_id}/"
            return lambda s, i: self._timed_request(s, "GET", url)

        if name == "detail_304":
            url = f"{base_url}/api/payments/{payment_id}/"
            headers = {"If-None-Match": etag}
            return lambda s, i: self._timed_request(s, "GET", url, headers=headers)

        if name in ("checkout", "replay"):
            url = f"{base_url}/api/checkout/sessions/"

            def checkout_task(session, i):
                idem_key = replay_key if replay_key else uuid.uuid4().hex
                headers = {"Idempotency-Key": idem_key}
                payload = {"amount": "10.00"}
                elapsed, code, timing, resp = self._timed_request(
                    session, "POST", url, json=payload, headers=headers, return_resp=True
                )
                if code == 400:
                    detail = str(resp.json().get("detail", "") if resp else "").lower()
                    if any(w in detail for w in ("provider", "disabled", "unavailable")):
                        raise CommandError(
                            "The fake provider is not enabled. Start the server with "
                            "ENABLE_FAKE_PROVIDER=True DEFAULT_PROVIDER=fake"
                        )
                return elapsed, code, timing

            return checkout_task

        raise CommandError(f"Unknown scenario: {name}")

    def _timed_request(self, session, method, url, return_resp=False, **kwargs):
        t0 = time.perf_counter()
        resp = session.request(method, url, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        timing = parse_server_timing(resp.headers.get("Server-Timing", ""))
        if return_resp:
            return elapsed, resp.status_code, timing, resp
        return elapsed, resp.status_code, timing

    def _print_table(self, results):
        has_timing = any(r.get("avg_db") is not None for r in results.values())
        header = f"{'Scenario':<12} {'Reqs':>6} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8} {'req/s':>8} {'throttled':>9} {'errors':>7}"
        if has_timing:
            header += f" {'db':>7} {'prov':>7} {'app':>7}"

        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for name, data in results.items():
            s = data["stats"]
            fmt = lambda v: f"{v:.1f}" if v is not None else "-"
            row = (
                f"{name:<12} {s['count']:>6} {fmt(s['p50']):>8} {fmt(s['p95']):>8} "
                f"{fmt(s['p99']):>8} {fmt(s['max']):>8} {fmt(s['requests_per_second']):>8} "
                f"{s['throttled']:>9} {s['errors']:>7}"
            )
            if has_timing:
                row += f" {fmt(data.get('avg_db')):>7} {fmt(data.get('avg_provider')):>7} {fmt(data.get('avg_app')):>7}"
            self.stdout.write(row)

    def handle(self, *args, **options):
        base_url = options["base_url"].rstrip("/")
        self._check_safety(base_url, options)

        count = options["requests"]
        concurrency = options["concurrency"]
        warmup = options["warmup"]
        target_scenario = options["scenario"]
        scenario_list = ["list", "detail", "detail_304", "checkout", "replay"] if target_scenario == "all" else [target_scenario]

        merchant = None
        try:
            # 1. Setup bench merchant and seed data
            merchant, raw_key = Merchant.create_with_key(name=f"bench-{uuid.uuid4().hex[:8]}")
            payments = [
                Payment.objects.create(
                    merchant=merchant,
                    idempotency_key=f"bench-seed-{i}-{uuid.uuid4().hex[:6]}",
                    amount=Decimal("15.00") + Decimal(i),
                    sender=f"customer-{i}@test.com",
                    receiver="bench-merchant",
                )
                for i in range(25)
            ]
            first_payment = payments[0]

            # 2. Prepare HTTP sessions (key in header, never printed)
            sessions = []
            for _ in range(concurrency):
                s = requests.Session()
                s.headers["X-API-Key"] = raw_key
                sessions.append(s)

            # Pre-fetch ETag for 304 scenario
            etag = None
            if "detail_304" in scenario_list:
                r = sessions[0].get(f"{base_url}/api/payments/{first_payment.id}/")
                etag = r.headers.get("ETag")

            # Pre-run one checkout session for replay scenario
            replay_key = None
            if "replay" in scenario_list:
                replay_key = uuid.uuid4().hex
                r = sessions[0].post(
                    f"{base_url}/api/checkout/sessions/",
                    json={"amount": "10.00"},
                    headers={"Idempotency-Key": replay_key},
                )
                if r.status_code == 400 and "provider" in r.text.lower():
                    raise CommandError(
                        "The fake provider is not enabled. Start the server with "
                        "ENABLE_FAKE_PROVIDER=True DEFAULT_PROVIDER=fake"
                    )

            results = {}
            checkout_durations = None
            first_durations = None

            # 3. Execute scenarios
            for sc in scenario_list:
                if sc == "detail_304" and not etag:
                    continue

                task = self._make_task(sc, base_url, first_payment.id, etag, replay_key)

                if warmup > 0:
                    self._run_scenario(task, warmup, concurrency, sessions)

                durs, errs, throttled, wall_sec, timing_samples, all_429 = self._run_scenario(
                    task, count, concurrency, sessions
                )

                if all_429 and throttled > 0 and not durs:
                    raise CommandError(
                        "All responses were 429 (throttled). Raise the throttle limits by "
                        "starting the server with:\n"
                        "  PAYMENT_THROTTLE_RATE=10000/minute\n"
                        "  PAYMENT_READ_THROTTLE_RATE=10000/minute"
                    )

                stats = summarize(durs, errs, throttled, wall_sec)
                avg_timing = {
                    f"avg_{k}": sum(v) / len(v) if v else None
                    for k, v in [
                        ("db", timing_samples.get("db")),
                        ("provider", timing_samples.get("provider")),
                        ("app", timing_samples.get("app")),
                    ]
                }
                results[sc] = {"stats": stats, **avg_timing}

                if sc == "checkout":
                    checkout_durations = durs
                if first_durations is None and durs:
                    first_durations = (sc, durs)

            # 4. Print clean report
            self.stdout.write("")
            self._print_table(results)

            # 5. Histogram for checkout (or first run scenario)
            hist_data = checkout_durations or (first_durations[1] if first_durations else None)
            hist_name = "checkout" if checkout_durations else (first_durations[0] if first_durations else None)
            if hist_data:
                self.stdout.write(f"\nLatency Histogram ({hist_name}):")
                self.stdout.write(render_histogram(histogram(hist_data, buckets=10)))

            self.stdout.write(f"\n{CAVEAT}\n")

            # 6. JSON output if requested
            if options["json_path"]:
                export = {sc: {**d["stats"], **{k: d[k] for k in ("avg_db", "avg_provider", "avg_app")}} for sc, d in results.items()}
                with open(options["json_path"], "w", encoding="utf-8") as f:
                    json.dump(export, f, indent=2)
                self.stdout.write(f"Metrics saved to {options['json_path']}")

            # 7. Check latency budget
            budget = options["budget_p95_ms"]
            if budget is not None:
                violations = [f"  {sc}: p95={d['stats']['p95']:.1f}ms > {budget:.1f}ms" for sc, d in results.items() if d['stats'].get('p95') and d['stats']['p95'] > budget]
                if violations:
                    raise CommandError("Budget exceeded for:\n" + "\n".join(violations))
                self.stdout.write(self.style.SUCCESS(f"All scenarios within p95 budget of {budget:.1f}ms."))

        finally:
            if merchant:
                Payment.objects.filter(merchant=merchant).delete()
                merchant.delete()
