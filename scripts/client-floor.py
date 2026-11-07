"""Measure the load harness's own overhead against a trivial endpoint.

Any latency reported by the harness includes this floor, so it is worth
knowing before reading a TTFT number as a server-side number.

Two modes:
  closed loop  - every worker fires back to back (worst case, saturates the client)
  open loop    - a fixed request rate, matching what a real scenario produces
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

import httpx


async def main(url: str, concurrency: int, duration: float, rate: float | None) -> None:
    samples: list[float] = []
    stop = time.monotonic() + duration

    async def closed_worker(client: httpx.AsyncClient) -> None:
        while time.monotonic() < stop:
            started = time.perf_counter()
            await client.get(url)
            samples.append((time.perf_counter() - started) * 1000)

    async def open_worker(client: httpx.AsyncClient, interval: float) -> None:
        next_at = time.monotonic()
        while time.monotonic() < stop:
            next_at += interval
            sleep_for = next_at - time.monotonic()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            started = time.perf_counter()
            try:
                await client.get(url)
            except httpx.HTTPError:
                continue
            samples.append((time.perf_counter() - started) * 1000)

    limits = httpx.Limits(max_connections=concurrency * 2 + 16)
    async with httpx.AsyncClient(limits=limits) as client:
        if rate:
            await asyncio.gather(open_worker(client, 1.0 / rate))
        else:
            await asyncio.gather(*[closed_worker(client) for _ in range(concurrency)])

    if not samples:
        print("no samples")
        return
    samples.sort()
    mode = f"open@{rate}rps" if rate else f"closed x{concurrency}"
    print(f"mode={mode} requests={len(samples)} rps={len(samples) / duration:.1f}")
    print(f"p50={statistics.median(samples):.2f}ms "
          f"p95={samples[int(len(samples) * 0.95)]:.2f}ms "
          f"max={samples[-1]:.2f}ms")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/healthz"
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 6
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
    rate = float(sys.argv[4]) if len(sys.argv) > 4 else None
    asyncio.run(main(target, concurrency, duration, rate))

