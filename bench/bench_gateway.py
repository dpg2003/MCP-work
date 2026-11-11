"""End-to-end gateway benchmark: throughput, latency, and event-loop health."""
import asyncio, sys, pathlib, tempfile, time, statistics
# Resolve the project from this file, not the working directory, so the
# benchmark runs correctly from anywhere.
ROOT = pathlib.Path(__file__).resolve().parent.parent / "fde-assessment" / "task4-rate-limit-router"
sys.path.insert(0, str(ROOT))
import httpx
from app import create_app
from providers import StubProvider
from rate_limiter import RateLimiter
from router import ModelRouter

KEYS = {"k": "tenant-a"}

async def main(concurrency=64, total=2000):
    db = tempfile.mktemp(suffix=".sqlite3")
    lim = RateLimiter(db, limit_tokens=10**12)
    app = create_app(limiter=lim,
                     router=ModelRouter(StubProvider("primary", tokens_used=25),
                                        StubProvider("secondary"), timeout_ms=1000),
                     api_keys=dict(KEYS))
    lat, ticks = [], []
    stop = False
    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter(); ticks.append((now-last)*1000); last = now

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://b.test") as c:
        await c.post("/v1/complete", json={"prompt":"warm","max_tokens":8}, headers={"X-API-Key":"k"})
        hb = asyncio.create_task(heartbeat())
        sem = asyncio.Semaphore(concurrency)
        async def one():
            async with sem:
                t0 = time.perf_counter()
                r = await c.post("/v1/complete", json={"prompt":"hello world","max_tokens":32},
                                 headers={"X-API-Key":"k"})
                lat.append((time.perf_counter()-t0)*1000)
                assert r.status_code == 200, r.text
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(total)])
        dt = time.perf_counter() - t0
        stop = True; await hb

    lat.sort(); ticks.sort()
    print(f"concurrency={concurrency} requests={total}")
    print(f"  throughput        {total/dt:8.0f} req/s")
    print(f"  latency  p50={lat[len(lat)//2]:6.2f}ms  p95={lat[int(len(lat)*.95)]:6.2f}ms  p99={lat[int(len(lat)*.99)]:6.2f}ms  max={lat[-1]:6.2f}ms")
    print(f"  loop heartbeat (target 5ms)  p50={statistics.median(ticks):5.2f}ms  p99={ticks[int(len(ticks)*.99)-1]:6.2f}ms  max={max(ticks):6.2f}ms")
    lim.close()

if __name__ == "__main__":
    import sys
    conc = int(sys.argv[1]) if len(sys.argv) > 1 else 64
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    asyncio.run(main(conc, total))
