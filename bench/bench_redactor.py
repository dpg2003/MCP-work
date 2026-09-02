"""Throughput benchmark for the streaming redactor."""
import sys, time, pathlib
sys.path.insert(0, str(pathlib.Path("fde-assessment/task3-pii-redaction-gateway").resolve()))
from redactor import StreamRedactor

PROSE = "The quick brown fox jumps over the lazy dog and keeps running onward. "
PII = "Contact john.doe@example.com or 123-45-6789 or 4111 1111 1111 1111. "

def run(label, chunk, n, chunk_size=None):
    text = chunk
    r = StreamRedactor()
    total = 0
    t0 = time.perf_counter()
    for _ in range(n):
        total += len(r.feed(text))
    total += len(r.close())
    dt = time.perf_counter() - t0
    chars = len(text) * n
    print(f"{label:34} {chars/1e6:6.2f} MB  {dt:6.3f}s  {chars/dt/1e6:7.2f} MB/s  peak_buf={r.peak_buffer_chars}")

def run_charwise(label, text, repeats):
    r = StreamRedactor()
    t0 = time.perf_counter()
    for _ in range(repeats):
        for ch in text:
            r.feed(ch)
    r.close()
    dt = time.perf_counter() - t0
    chars = len(text) * repeats
    print(f"{label:34} {chars/1e6:6.2f} MB  {dt:6.3f}s  {chars/dt/1e6:7.2f} MB/s")

if __name__ == "__main__":
    run("prose, 70-char chunks",   PROSE, 40_000)
    run("pii-heavy, 68-char chunks", PII,  40_000)
    run("prose, 1-char chunks",    "a",   200_000)
    run("digits (adversarial)",    "1"*70, 40_000)
    run_charwise("pii streamed char-by-char", PII, 2_000)
