"""Throughput and event-loop-blocking benchmark for the rate limiter."""
import asyncio, sys, pathlib, tempfile, time, statistics
# Resolve the project from this file, not the working directory, so the
# benchmark runs correctly from anywhere.
PROJECT = pathlib.Path(__file__).resolve().parent.parent / "fde-assessment" / "task4-rate-limit-router"
sys.path.insert(0, str(PROJECT))
from rate_limiter import RateLimiter

def sync_throughput(n=3000):
    db = tempfile.mktemp(suffix=".sqlite3")
    lim = RateLimiter(db, limit_tokens=10**12)
    t0 = time.perf_counter()
    for i in range(n):
        lim.try_consume(f"t{i%10}", 10)
    dt = time.perf_counter() - t0
    print(f"sync try_consume        {n/dt:9.0f} ops/s   ({dt*1e3/n:.3f} ms/op)   rows={lim.row_count()}")
    lim.close()

async def loop_blocking(n=600):
    """Measure event-loop stall: a 5ms heartbeat should tick ~n times."""
    db = tempfile.mktemp(suffix=".sqlite3")
    lim = RateLimiter(db, limit_tokens=10**12)
    ticks, stop = [], False
    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter(); ticks.append((now-last)*1000); last = now
    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    t0 = time.perf_counter()
    for i in range(n):
        lim.try_consume(f"t{i%10}", 10)      # blocking call on the loop
    dt = time.perf_counter() - t0
    stop = True; await hb
    p = sorted(ticks)
    print(f"blocking-on-loop        {n/dt:9.0f} ops/s   heartbeat p50={statistics.median(p):.1f}ms "
          f"p99={p[int(len(p)*0.99)-1]:.1f}ms max={max(p):.1f}ms  (target 5ms)")
    lim.close()

if __name__ == "__main__":
    sync_throughput()
    asyncio.run(loop_blocking())
