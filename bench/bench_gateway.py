"""End-to-end gateway benchmark: throughput, latency, and event-loop health."""
import asyncio, os, sys, pathlib, tempfile, time, statistics
# Resolve the project from this file, not the working directory, so the
# benchmark runs correctly from anywhere.
ROOT = pathlib.Path(__file__).resolve().parent.parent / "fde-assessment" / "task4-rate-limit-router"
sys.path.insert(0, str(ROOT))
# Importing app.py builds its module-level `app`, whose limiter opens
# RATE_LIMIT_DB (default: rate_limit.sqlite3 in the working directory). The
# benchmark never uses that app, so keep its database in memory and leave no
# file behind.
os.environ["RATE_LIMIT_DB"] = ":memory:"
import httpx
from app import create_app
from providers import StubProvider
from rate_limiter import RateLimiter
from router import ModelRouter
from tenants import TenantRegistry

API_KEY = "k"
# This measures gateway throughput, not admission control. The benchmark tenant
# still goes through every limit check on every request; its quotas are just
# set far above any load this script can generate, so none of them refuses.
UNREACHABLE = 10**12


def bench_registry() -> TenantRegistry:
    """One tenant whose token, request and concurrency quotas are never reached."""
    return TenantRegistry.from_dict({
        "tiers": {"bench": {"tokens_per_minute": UNREACHABLE,
                            "requests_per_minute": UNREACHABLE,
                            "max_concurrent": UNREACHABLE}},
        "tenants": [{"tenant_id": "tenant-a", "tier": "bench", "api_keys": [API_KEY]}],
    })

async def main(concurrency=64, total=2000):
    # A private directory, so the database and its -wal/-shm files are removed
    # afterwards (and at interpreter exit even if a request fails).
    tmp = tempfile.TemporaryDirectory()
    db = os.path.join(tmp.name, "bench.sqlite3")
    lim = RateLimiter(db, limit_tokens=10**12)
    app = create_app(limiter=lim,
                     router=ModelRouter(StubProvider("primary", tokens_used=25),
                                        StubProvider("secondary"), timeout_ms=1000),
                     registry=bench_registry())
    lat, ticks = [], []
    stop = False
    async def heartbeat():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.005)
            now = time.perf_counter(); ticks.append((now-last)*1000); last = now

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://b.test") as c:
        await c.post("/v1/complete", json={"prompt":"warm","max_tokens":8}, headers={"X-API-Key": API_KEY})
        hb = asyncio.create_task(heartbeat())
        sem = asyncio.Semaphore(concurrency)
        async def one():
            async with sem:
                t0 = time.perf_counter()
                r = await c.post("/v1/complete", json={"prompt":"hello world","max_tokens":32},
                                 headers={"X-API-Key": API_KEY})
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
    tmp.cleanup()

if __name__ == "__main__":
    import sys
    conc = int(sys.argv[1]) if len(sys.argv) > 1 else 64
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
    asyncio.run(main(conc, total))
