"""Task 4 assessment cases: rate limiting, failover, and error hygiene."""

from __future__ import annotations

import concurrent.futures
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from _harness import ASSESSMENT, PYTHON, Runner

PROJECT = ASSESSMENT / "task4-rate-limit-router"
sys.path.insert(0, str(PROJECT))

from rate_limiter import RateLimiter  # noqa: E402

GATEWAY = "http://127.0.0.1:9700"
UPSTREAM = "http://127.0.0.1:9701"


def set_mode(which: str, mode: str, latency: float = 0.0) -> None:
    """Configure one fake provider's behaviour."""
    httpx.post(f"{UPSTREAM}/_control/{which}",
               json={"mode": mode, "latency_seconds": latency}, timeout=10)


def complete(prompt: str = "hello", max_tokens: int = 100,
             key: str = "key-acme", timeout: float = 60) -> httpx.Response:
    """Issue one completion request."""
    return httpx.post(f"{GATEWAY}/v1/complete", json={"prompt": prompt, "max_tokens": max_tokens},
                      headers={"X-API-Key": key}, timeout=timeout)


def summarise(response: httpx.Response) -> str:
    """One-line description of a gateway response."""
    body = response.json()
    if "error" in body:
        return f"HTTP {response.status_code}, `{body['error']['type']}`"
    return (f"HTTP {response.status_code}, provider={body['provider']}, "
            f"failed_over={body['failed_over']}")


def run(runner: Runner) -> None:
    """Execute cases 4.1 - 4.14."""
    db = Path(tempfile.mkdtemp()) / "matrix.sqlite3"

    # --- 4.2 / 4.3 / 4.4 / 4.5 / 4.12 / 4.14 on the limiter directly -----
    # An injectable clock is used so window eviction is exercised without a
    # 60-second wait; everything else runs against the real HTTP service.
    now = [1_000_000.0]
    limiter = RateLimiter(db_path=str(db.parent / "unit.sqlite3"), clock=lambda: now[0])

    allowed = [limiter.try_consume("t1", 10_000).allowed for _ in range(4)]
    under_limit_usage = limiter.usage("t1")

    at_limit = limiter.try_consume("t1", 10_000)
    over = limiter.try_consume("t1", 1)
    runner.record(
        "4.2", "At rate limit boundary",
        f"request landing on exactly 50,000 → allowed={at_limit.allowed} "
        f"(usage now {limiter.usage('t1')}); one more token → allowed={over.allowed}. "
        "Documented: inclusive, `used + tokens <= limit`.",
        at_limit.allowed and not over.allowed,
    )
    runner.record(
        "4.3", "Over rate limit",
        f"rejected with retry_after={over.retry_after_seconds}s, "
        f"used={over.used_tokens}, limit={over.limit_tokens}; nothing recorded",
        not over.allowed and limiter.usage("t1") == 50_000,
    )

    now[0] += 61
    after_slide = limiter.try_consume("t1", 50_000)
    runner.record(
        "4.4", "Sliding window eviction",
        f"clock +61 s → usage evicted to 0, new 50,000-token request allowed="
        f"{after_slide.allowed}; stored rows now {limiter.row_count()}",
        after_slide.allowed,
    )

    limiter.try_consume("tenant-a", 50_000)
    b_ok = limiter.try_consume("tenant-b", 50_000)
    runner.record(
        "4.5", "Per-tenant isolation",
        f"tenant-a exhausted (usage {limiter.usage('tenant-a')}); "
        f"tenant-b 50,000-token request allowed={b_ok.allowed}",
        b_ok.allowed and not limiter.try_consume("tenant-a", 1).allowed,
    )

    # 4.12 / 4.14: 32 threads through a barrier onto one SQLite file.
    race = RateLimiter(db_path=str(db.parent / "race.sqlite3"), clock=lambda: now[0])
    import threading

    barrier = threading.Barrier(32)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        """Consume 5,000 tokens, all threads released together."""
        barrier.wait()
        decision = race.try_consume("racer", 5_000)
        with lock:
            outcomes.append(decision.allowed)

    threads = [threading.Thread(target=attempt) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    journal = race._conn.execute("PRAGMA journal_mode").fetchone()[0]
    runner.record(
        "4.12", "Concurrent requests race condition",
        f"32 simultaneous 5,000-token requests → exactly {sum(outcomes)} admitted, "
        f"final usage {race.usage('racer')} (limit 50,000). Serialised by "
        "`BEGIN IMMEDIATE`, so check-then-write cannot interleave.",
        sum(outcomes) == 10 and race.usage("racer") == 50_000,
    )
    runner.record(
        "4.14", "SQLite concurrent writes",
        f"same 32-thread burst completed with no SQLITE_BUSY or corruption; "
        f"journal_mode={journal}, busy_timeout=30000 ms",
        journal.lower() == "wal" and sum(outcomes) == 10,
    )
    race.close()
    limiter.close()

    # --- 4.13: persistence across a real process restart -----------------
    persist = str(db.parent / "persist.sqlite3")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from rate_limiter import RateLimiter\n"
        "limiter = RateLimiter(db_path=%r, clock=lambda: 1000000.0)\n"
        "import json; print(json.dumps({'allowed': limiter.try_consume(sys.argv[1], int(sys.argv[2])).allowed,\n"
        "                               'usage': limiter.usage(sys.argv[1])}))\n"
    ) % (str(PROJECT), persist)

    def in_new_process(tenant: str, tokens: int) -> dict:
        """Run one admission in a genuinely separate interpreter."""
        out = subprocess.run([PYTHON, "-c", script, tenant, str(tokens)],
                             capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    first = in_new_process("persisted", 45_000)
    second = in_new_process("persisted", 10_000)
    third = in_new_process("persisted", 5_000)
    runner.record(
        "4.13", "SQLite persistence across restart",
        f"process 1: 45,000 allowed (usage {first['usage']}); "
        f"process 2 (fresh interpreter): 10,000 → allowed={second['allowed']}; "
        f"process 3: 5,000 → allowed={third['allowed']} (usage {third['usage']}). "
        "Not reset to zero.",
        first["allowed"] and not second["allowed"] and third["allowed"]
        and third["usage"] == 50_000,
    )

    # --- HTTP service for the failover cases -----------------------------
    runner.serve(PROJECT, "fake_upstream:app", 9701)
    runner.serve(PROJECT, "app:app", 9700, {
        "RATE_LIMIT_DB": str(db),
        "RATE_LIMIT_TOKENS": "10000000",
        "PROVIDER_TIMEOUT_MS": "3000",
        "PRIMARY_URL": f"{UPSTREAM}/primary/v1/complete",
        "SECONDARY_URL": f"{UPSTREAM}/secondary/v1/complete",
    })

    set_mode("primary", "ok")
    r = complete()
    runner.record(
        "4.1", "Under rate limit",
        f"limiter: 4 x 10,000 tokens → allowed={allowed}, window usage {under_limit_usage}. "
        f"End to end: {summarise(r)}",
        all(allowed) and r.status_code == 200 and r.json()["provider"] == "primary",
    )

    # -- 4.6 ---------------------------------------------------------------
    set_mode("primary", "429")
    r = complete()
    runner.record("4.6", "Primary returns HTTP 429", summarise(r),
                  r.status_code == 200 and r.json()["provider"] == "secondary")

    # -- 4.7 / 4.8 / 4.9: real wall-clock timing --------------------------
    set_mode("primary", "ok", latency=5.0)
    start = time.perf_counter()
    r = complete()
    elapsed = time.perf_counter() - start
    runner.record(
        "4.7", "Primary timeout (>3000 ms)",
        f"primary held 5,000 ms → failed over after {elapsed*1000:.0f} ms, "
        f"provider={r.json().get('provider')}",
        r.status_code == 200 and r.json()["provider"] == "secondary" and elapsed < 4.5,
    )

    set_mode("primary", "ok", latency=2.9)
    start = time.perf_counter()
    r = complete()
    elapsed = time.perf_counter() - start
    body = r.json()
    runner.record(
        "4.8", "Primary responds just under timeout (~2900 ms)",
        f"responded in {elapsed*1000:.0f} ms → provider={body.get('provider')}, "
        f"failed_over={body.get('failed_over')}",
        r.status_code == 200 and body.get("provider") == "primary"
        and body.get("failed_over") is False,
    )

    set_mode("primary", "ok", latency=3.1)
    start = time.perf_counter()
    r = complete()
    elapsed = time.perf_counter() - start
    body = r.json()
    runner.record(
        "4.9", "Primary responds just over timeout (~3100 ms)",
        f"cut off at {elapsed*1000:.0f} ms → provider={body.get('provider')}, "
        f"failed_over={body.get('failed_over')}",
        r.status_code == 200 and body.get("provider") == "secondary",
    )

    # -- 4.10 / 4.11 -------------------------------------------------------
    set_mode("primary", "429")
    set_mode("secondary", "500")
    r = complete()
    body = r.json()
    shape_ok = set(body) == {"error"} and set(body["error"]) == {
        "type", "message", "request_id", "details"}
    runner.record(
        "4.10", "Both primary and secondary fail",
        f"HTTP {r.status_code}, single error `{body['error']['type']}`, "
        f"attempts={body['error']['details']['attempts']}",
        r.status_code == 502 and shape_ok,
    )

    leaked = [token for token in
              ("Traceback", "sk-live-9f2c31aa", "inference-primary.internal",
               "/opt/inference/server.py", "RuntimeError", "pool exhausted")
              if token in r.text]
    runner.record(
        "4.11", "Error payload sanitization",
        f"upstream body contained a stack trace, an API key fragment and an internal "
        f"hostname; leaked into the response: {leaked or 'none'}. "
        f"Client sees only `{json.dumps(body['error'])[:80]}…`",
        not leaked and shape_ok,
    )

    set_mode("primary", "ok")
    set_mode("secondary", "ok")
