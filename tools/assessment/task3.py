"""Task 3 assessment cases: streaming PII redaction."""

from __future__ import annotations

import sys
import time
import tracemalloc

import httpx

from _harness import ASSESSMENT, Runner

PROJECT = ASSESSMENT / "task3-pii-redaction-gateway"
sys.path.insert(0, str(PROJECT))

from redactor import StreamRedactor  # noqa: E402


def stream(chunks: list[str]) -> str:
    """Feed chunks through the redactor and return the emitted output."""
    redactor = StreamRedactor()
    return "".join([redactor.feed(c) for c in chunks]) + redactor.close()


def run(runner: Runner) -> None:
    """Execute cases 3.1 - 3.12."""
    # -- 3.1 / 3.2 / 3.3 ---------------------------------------------------
    out = stream(["Contact me at john@example.com please"])
    runner.record("3.1", "Email in single chunk", f"`{out}`",
                  out == "Contact me at [REDACTED] please")

    out = stream(["SSN: 123-45-6789"])
    runner.record("3.2", "SSN in single chunk", f"`{out}`", out == "SSN: [REDACTED]")

    dashed = stream(["4111-1111-1111-1111"])
    bare = stream(["4111111111111111"])
    runner.record("3.3", "Credit card in single chunk",
                  f"dashed → `{dashed}`; bare → `{bare}`",
                  dashed == "[REDACTED]" and bare == "[REDACTED]")

    # -- 3.4 / 3.5 ---------------------------------------------------------
    out = stream(["my email is john@exam", "ple.com thanks"])
    runner.record("3.4", "PII split across two chunks", f"`{out}`",
                  out == "my email is [REDACTED] thanks")

    out = stream(["SSN 123-45-", "6789 is mine"])
    runner.record("3.5", "PII split mid-token (SSN)", f"`{out}`",
                  out == "SSN [REDACTED] is mine")

    # -- 3.6 ---------------------------------------------------------------
    out = stream(["Reach a@b.co", "m, ssn 987-", "65-4321, card 4111 1111 ",
                  "1111 1111. Ticket 123456789 stays."])
    ok = (out.count("[REDACTED]") == 3 and "Ticket 123456789 stays." in out
          and "@" not in out and "4111" not in out)
    runner.record("3.6", "Multiple PII types across chunks", f"`{out}`", ok)

    # -- 3.7 ---------------------------------------------------------------
    text = "The quick brown fox jumps over the lazy dog. Nothing sensitive here at all."
    out = stream([text[i:i + 7] for i in range(0, len(text), 7)])
    runner.record("3.7", "No PII present", f"unchanged: {out == text}", out == text)

    # -- 3.8 ---------------------------------------------------------------
    near = {
        "Order number 123-45-6789X": "trailing X breaks the word boundary",
        "call 555-123-4567": "phone number, 3-3-4 not 3-2-4",
        "host 192.168.1.1": "dots are not accepted as separators",
        "order 1234567812345678": "16 digits but fails Luhn",
        "product code 123456789": "bare 9 digits, no separators",
    }
    results = {t: stream([t]) == t for t in near}
    runner.record(
        "3.8", "False-positive check",
        "; ".join(f"`{t[:26]}` → {'unchanged' if ok else 'REDACTED'}" for t, ok in results.items()),
        all(results.values()),
    )

    # -- 3.9: memory ------------------------------------------------------
    redactor = StreamRedactor()
    body = "Ordinary streamed model output that carries no personal data at all. "
    for _ in range(2_000):
        redactor.feed(body)
    tracemalloc.start()
    baseline = tracemalloc.get_traced_memory()[0]
    total = 0
    for index in range(40_000):     # ~2.7 MB, far beyond 10k tokens
        chunk = body if index % 200 else f"leak john.doe@example.com and 123-45-6789 "
        total += len(chunk)
        redactor.feed(chunk)
    redactor.close()
    growth = tracemalloc.get_traced_memory()[0] - baseline
    tracemalloc.stop()
    runner.record(
        "3.9", "Memory — no full buffering",
        f"{total/1e6:.1f} MB streamed; retained growth {growth/1024:.1f} KiB; "
        f"peak buffer {redactor.peak_buffer_chars} chars (cap {redactor.max_hold} + one chunk)",
        growth < 200_000 and redactor.peak_buffer_chars <= redactor.max_hold + len(body),
    )

    # -- 3.10 / 3.11: live server, real sockets ---------------------------
    runner.serve(PROJECT, "app:app", 9600, {"MOCK_CHUNK_DELAY_SECONDS": "0.05"})
    with httpx.Client(base_url="http://127.0.0.1:9600", timeout=60) as client:
        start = time.perf_counter()
        first_at = None
        arrivals = []
        with client.stream("POST", "/v1/generate", json={"prompt": "summarise"}) as response:
            for chunk in response.iter_raw():
                if not chunk:
                    continue
                now = time.perf_counter() - start
                if first_at is None:
                    first_at = now
                arrivals.append(now)
        total_time = time.perf_counter() - start

    runner.record(
        "3.10", "Time to First Token",
        f"TTFT {first_at*1000:.0f} ms of {total_time*1000:.0f} ms total "
        f"(upstream emits a chunk every 50 ms, so ~one chunk of delay, not the whole stream)",
        first_at < total_time / 3,
    )
    runner.record(
        "3.11", "Stream remains chunked",
        f"{len(arrivals)} separate chunks delivered, first at {arrivals[0]*1000:.0f} ms, "
        f"last at {arrivals[-1]*1000:.0f} ms",
        len(arrivals) > 1,
    )

    # -- 3.12 --------------------------------------------------------------
    held = stream(["Here it is: ", "john.doe@exam", "ple.com"])
    trailing_clean = stream(["Nothing sensitive ", "in this tail"])
    runner.record(
        "3.12", "Redaction at end-of-stream buffer flush",
        f"PII in held tail → `{held}`; clean tail → `{trailing_clean}` (nothing dropped)",
        held == "Here it is: [REDACTED]" and trailing_clean == "Nothing sensitive in this tail",
    )
