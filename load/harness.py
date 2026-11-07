from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from load.report import build_timeseries, summarise, to_markdown

_FILLER = (
    "Summarise the following operational context for an on-call engineer: "
    "queue depth, token throughput, p95 latency, upstream error budget, "
    "circuit state, admission control limits and cache hit ratio. "
)


@dataclass
class RequestResult:
    started_at: float
    latency_ms: float
    ttft_ms: float | None
    ttfb_ms: float | None
    tpot_ms: float | None
    status: int
    error: str | None
    ok: bool
    phase: str
    cached: bool
    upstream: str
    completion_tokens: int


@dataclass
class Config:
    base_url: str
    duration_s: float
    concurrency: int
    ramp_s: float
    warmup_s: float
    stream: bool
    tenant: str
    prompt_tokens: int
    max_tokens: int
    unique_prompts: int
    timeout_s: float
    model: str | None
    chaos_url: str | None
    chaos_at: float
    chaos_mode: str
    chaos_rate: float
    chaos_duration_s: float
    rate: float | None


class Pacer:
    """Constant-arrival-rate pacer (open loop).

    A closed-loop generator measures "as fast as the client can go", which
    mostly measures the client's own limits and the server's rate limiter.
    Pacing to a target rate measures the server's behaviour at a known
    offered load instead.
    """

    def __init__(self, rate_per_sec: float) -> None:
        self.interval = 1.0 / rate_per_sec
        self._next_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            target = max(now, self._next_at)
            self._next_at = target + self.interval
        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


class Harness:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.results: list[RequestResult] = []
        self.prompts = self._build_prompts()
        self.started_at = 0.0
        self.warmup_end = 0.0
        self.measured_end = 0.0
        self.pacer = Pacer(config.rate) if config.rate else None

    def _build_prompts(self) -> list[str]:
        base = self.config.prompt_tokens * 4
        prompts = []
        for index in range(max(1, self.config.unique_prompts)):
            marker = f"[scenario-{index:03d}]"
            body = (marker + _FILLER) * max(1, base // len(_FILLER) + 1)
            prompts.append(body[:base])
        return prompts

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": random.choice(self.prompts)}],
            "max_tokens": self.config.max_tokens,
            "temperature": 0.0,
            "stream": self.config.stream,
        }
        if self.config.model:
            payload["model"] = self.config.model
        return payload

    def _headers(self) -> dict[str, str]:
        return {"x-tenant-id": self.config.tenant, "content-type": "application/json"}

    @staticmethod
    def _classify_status(status: int, body: str) -> str:
        try:
            parsed = json.loads(body)
            return parsed.get("error", {}).get("type", f"http_{status}")
        except (ValueError, AttributeError):
            return f"http_{status}"

    async def _run_blocking(
        self, client: httpx.AsyncClient, phase: str
    ) -> RequestResult:
        started = time.perf_counter()
        wall = time.time()
        status = 0
        error: str | None = None
        cached = False
        upstream = ""
        tokens = 0
        try:
            response = await client.post(
                f"{self.config.base_url}/v1/chat/completions",
                json=self._payload(),
                headers=self._headers(),
            )
            status = response.status_code
            cached = response.headers.get("x-cache") == "HIT"
            upstream = response.headers.get("x-upstream", "")
            if status == 200:
                try:
                    body = response.json()
                    tokens = int((body.get("usage") or {}).get("completion_tokens", 0))
                except ValueError:
                    tokens = 0
            else:
                error = self._classify_status(status, response.text)
        except httpx.TimeoutException:
            error = "client_timeout"
        except httpx.HTTPError as exc:
            error = f"transport:{type(exc).__name__}"
        latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            started_at=wall,
            latency_ms=round(latency_ms, 2),
            ttft_ms=None,
            ttfb_ms=None,
            tpot_ms=None,
            status=status,
            error=error,
            ok=error is None and status == 200,
            phase=phase,
            cached=cached,
            upstream=upstream,
            completion_tokens=tokens,
        )

    async def _run_streaming(
        self, client: httpx.AsyncClient, phase: str
    ) -> RequestResult:
        started = time.perf_counter()
        wall = time.time()
        status = 0
        error: str | None = None
        cached = False
        upstream = ""
        tokens = 0
        first_byte_at: float | None = None
        first_content_at: float | None = None
        saw_done = False
        try:
            async with client.stream(
                "POST",
                f"{self.config.base_url}/v1/chat/completions",
                json=self._payload(),
                headers=self._headers(),
            ) as response:
                status = response.status_code
                cached = response.headers.get("x-cache") == "HIT"
                upstream = response.headers.get("x-upstream", "")
                if status != 200:
                    await response.aread()
                    error = f"http_{status}"
                else:
                    # Deliberately not parsing JSON per chunk: the harness has to
                    # measure the server, not become the bottleneck itself.
                    buffer = b""
                    async for raw in response.aiter_bytes():
                        now = time.perf_counter()
                        if first_byte_at is None:
                            first_byte_at = now
                        buffer += raw
                        while b"\n" in buffer:
                            line, _, buffer = buffer.partition(b"\n")
                            stripped = line.strip()
                            if stripped == b"data: [DONE]":
                                saw_done = True
                                break
                            if stripped.startswith(b"event: error"):
                                error = "upstream_stream_error"
                                continue
                            if not stripped.startswith(b"data:"):
                                continue
                            # Whitespace inside the JSON varies by producer, so
                            # compare against a space-free copy of the frame.
                            compact = stripped.replace(b" ", b"")
                            if b'"content":"' in compact and b'"content":""' not in compact:
                                if first_content_at is None:
                                    first_content_at = now
                                tokens += 1
                        if saw_done:
                            break
                    if not saw_done and error is None:
                        error = "stream_truncated"
        except httpx.TimeoutException:
            error = "client_timeout"
        except httpx.HTTPError as exc:
            error = f"transport:{type(exc).__name__}"
        latency_ms = (time.perf_counter() - started) * 1000
        tpot_ms = None
        if first_content_at is not None and tokens > 1:
            tpot_ms = round((latency_ms - (first_content_at - started) * 1000) / (tokens - 1), 2)
        return RequestResult(
            started_at=wall,
            latency_ms=round(latency_ms, 2),
            ttft_ms=round((first_content_at - started) * 1000, 2)
            if first_content_at
            else None,
            ttfb_ms=round((first_byte_at - started) * 1000, 2)
            if first_byte_at
            else None,
            tpot_ms=tpot_ms,
            status=status,
            error=error,
            ok=error is None and status == 200,
            phase=phase,
            cached=cached,
            upstream=upstream,
            completion_tokens=tokens,
        )

    async def _worker(self, client: httpx.AsyncClient, index: int) -> None:
        config = self.config
        if config.ramp_s > 0:
            offset = config.ramp_s * (index / max(1, config.concurrency))
            await asyncio.sleep(offset)
        while True:
            now = time.time()
            if now >= self.warmup_end + config.duration_s:
                return
            if self.pacer is not None:
                await self.pacer.wait()
            phase = "warmup" if now < self.warmup_end else "measured"
            if config.stream:
                result = await self._run_streaming(client, phase)
            else:
                result = await self._run_blocking(client, phase)
            self.results.append(result)

    async def _chaos_task(self) -> None:
        config = self.config
        if not config.chaos_url or config.chaos_mode == "none":
            return
        delay = config.chaos_at - (time.time() - self.started_at)
        if delay > 0:
            await asyncio.sleep(delay)
        body = {
            "mode": config.chaos_mode,
            "rate": config.chaos_rate,
            "duration_s": config.chaos_duration_s,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{config.chaos_url}/admin/faults", json=body
                )
                print(
                    f"[chaos] injected {config.chaos_mode} rate={config.chaos_rate} "
                    f"-> HTTP {response.status_code}",
                    file=sys.stderr,
                )
        except httpx.HTTPError as exc:
            print(f"[chaos] failed to inject fault: {exc}", file=sys.stderr)

    async def run(self) -> dict[str, Any]:
        config = self.config
        limits = httpx.Limits(
            max_connections=config.concurrency * 2 + 16,
            max_keepalive_connections=config.concurrency + 16,
        )
        timeout = httpx.Timeout(
            connect=10.0, read=config.timeout_s, write=10.0, pool=30.0
        )
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            self.started_at = time.time()
            self.warmup_end = self.started_at + config.warmup_s
            tasks = [
                asyncio.create_task(self._worker(client, index))
                for index in range(config.concurrency)
            ]
            chaos = asyncio.create_task(self._chaos_task())
            await asyncio.gather(*tasks)
            self.measured_end = time.time()
            chaos.cancel()
            await asyncio.gather(chaos, return_exceptions=True)
        return await self.build_report()

    async def build_report(self) -> dict[str, Any]:
        config = self.config
        measured = [r for r in self.results if r.phase == "measured"]
        window = max(1e-6, self.measured_end - (self.warmup_end))
        successful = [r for r in measured if r.ok]
        failed = [r for r in measured if not r.ok]

        latencies = [r.latency_ms for r in measured]
        ttfts = [r.ttft_ms for r in measured if r.ttft_ms is not None]
        ttfbs = [r.ttfb_ms for r in measured if r.ttfb_ms is not None]
        tpots = [r.tpot_ms for r in measured if r.tpot_ms is not None]

        status_codes: dict[str, int] = {}
        errors: dict[str, int] = {}
        upstreams: dict[str, int] = {}
        cache_hits = 0
        total_tokens = 0
        decode_seconds = 0.0

        for result in measured:
            status_codes[str(result.status)] = status_codes.get(str(result.status), 0) + 1
            if result.error:
                errors[result.error] = errors.get(result.error, 0) + 1
            upstreams[result.upstream or "unknown"] = (
                upstreams.get(result.upstream or "unknown", 0) + 1
            )
            if result.cached:
                cache_hits += 1
            total_tokens += result.completion_tokens
            if result.ttft_ms is not None:
                decode_seconds += max(0.0, (result.latency_ms - result.ttft_ms) / 1000.0)

        summary = {
            "requests": len(measured),
            "successful": len(successful),
            "failed": len(failed),
            "error_rate": round(len(failed) / len(measured), 5) if measured else 0.0,
            "achieved_rps": round(len(measured) / window, 2) if measured else 0.0,
            "latency": asdict(summarise(latencies)),
            "ttft": asdict(summarise(ttfts)),
            "ttfb": asdict(summarise(ttfbs)),
            "tpot": asdict(summarise(tpots)),
            "status_codes": status_codes,
            "errors": errors,
            "upstreams": upstreams,
            "cache": {
                "hits": cache_hits,
                "misses": len(measured) - cache_hits,
                "hit_ratio": round(cache_hits / len(measured), 4) if measured else 0.0,
            },
            "output_tokens": total_tokens,
            "output_tokens_per_second": round(
                total_tokens / decode_seconds, 2
            ) if decode_seconds > 0 else 0.0,
        }

        meta = {
            "base_url": config.base_url,
            "stream": config.stream,
            "concurrency": config.concurrency,
            "ramp_s": config.ramp_s,
            "warmup_s": config.warmup_s,
            "duration_s": config.duration_s,
            "tenant": config.tenant,
            "prompt_tokens": config.prompt_tokens,
            "max_tokens": config.max_tokens,
            "unique_prompts": config.unique_prompts,
            "rate": config.rate,
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if config.chaos_mode != "none":
            meta["chaos"] = {
                "mode": config.chaos_mode,
                "rate": config.chaos_rate,
                "at_s": config.chaos_at,
                "duration_s": config.chaos_duration_s,
                "target": config.chaos_url,
            }

        report = {
            "meta": meta,
            "summary": summary,
            "timeseries": [asdict(p) for p in build_timeseries(measured)],
        }
        report["server_status"] = await _fetch_server_status(config.base_url)
        return report


async def _fetch_server_status(base_url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/admin/status")
            if response.status_code == 200:
                return response.json()
    except httpx.HTTPError:
        return None
    return None


def _print_console(report: dict[str, Any]) -> None:
    summary = report["summary"]
    latency = summary["latency"]
    ttft = summary["ttft"]
    meta = report["meta"]
    width = 62
    print("=" * width)
    print(f"LOAD TEST  {meta['base_url']}  "
          f"{'streaming' if meta['stream'] else 'blocking'}")
    if meta.get("rate"):
        print(f"offered rate: {meta['rate']} rps (constant arrival)")
    else:
        print("offered rate: closed loop (as fast as the client can go)")
    print("=" * width)
    print(f"{'requests':<22}{summary['requests']}")
    print(f"{'successful':<22}{summary['successful']}")
    print(f"{'failed':<22}{summary['failed']}")
    print(f"{'error rate':<22}{summary['error_rate'] * 100:.2f}%")
    print(f"{'achieved rps':<22}{summary['achieved_rps']:.2f}")
    print(f"{'output tok/s (mean)':<22}{summary['output_tokens_per_second']:.1f}")
    print("-" * width)
    print(f"{'latency p50':<22}{latency['p50_ms']} ms")
    print(f"{'latency p95':<22}{latency['p95_ms']} ms")
    print(f"{'latency p99':<22}{latency['p99_ms']} ms")
    print(f"{'latency max':<22}{latency['max_ms']} ms")
    if ttft["count"]:
        print(f"{'ttft p50':<22}{ttft['p50_ms']} ms")
        print(f"{'ttft p95':<22}{ttft['p95_ms']} ms")
    tpot = summary.get("tpot", {})
    if tpot.get("count"):
        print(f"{'inter-token p50':<22}{tpot['p50_ms']} ms")
        print(f"{'inter-token p95':<22}{tpot['p95_ms']} ms")
    print("-" * width)
    if summary["status_codes"]:
        print("status codes: " + ", ".join(
            f"{code}={count}" for code, count in sorted(summary["status_codes"].items())
        ))
    if summary["errors"]:
        print("errors: " + ", ".join(
            f"{name}={count}" for name, count in sorted(summary["errors"].items())
        ))
    if summary["upstreams"]:
        print("routing: " + ", ".join(
            f"{name}={count}" for name, count in sorted(summary["upstreams"].items())
        ))
    cache = summary["cache"]
    print(f"cache: hits={cache['hits']} misses={cache['misses']} "
          f"ratio={cache['hit_ratio'] * 100:.1f}%")
    print("=" * width)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load test harness for the LLM gateway.",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--ramp", type=float, default=5.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--blocking", action="store_true", help="disable streaming")
    parser.add_argument("--tenant", default="loadtest")
    parser.add_argument("--prompt-tokens", type=int, default=180)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--unique-prompts", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default="reports/load-latest.json")
    parser.add_argument("--chaos-url", default=None)
    parser.add_argument("--chaos-at", type=float, default=10.0)
    parser.add_argument("--chaos-mode", default="none")
    parser.add_argument("--chaos-rate", type=float, default=0.5)
    parser.add_argument("--chaos-duration", type=float, default=15.0)
    parser.add_argument(
        "--rate",
        type=float,
        default=None,
        help="Target requests per second (constant arrival rate). "
        "Omit to run closed-loop (as fast as the client can go).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = Config(
        base_url=args.url.rstrip("/"),
        duration_s=args.duration,
        concurrency=args.concurrency,
        ramp_s=args.ramp,
        warmup_s=args.warmup,
        stream=not args.blocking,
        tenant=args.tenant,
        prompt_tokens=args.prompt_tokens,
        max_tokens=args.max_tokens,
        unique_prompts=args.unique_prompts,
        timeout_s=args.timeout,
        model=args.model,
        chaos_url=args.chaos_url,
        chaos_at=args.chaos_at,
        chaos_mode=args.chaos_mode,
        chaos_rate=args.chaos_rate,
        chaos_duration_s=args.chaos_duration,
        rate=args.rate,
    )
    harness = Harness(config)
    report = asyncio.run(harness.run())

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path = out_path.with_suffix(".md")
    md_path.write_text(to_markdown(report), encoding="utf-8")

    _print_console(report)
    print(f"report: {out_path}")
    print(f"report: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
