from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class LatencyStats:
    count: int = 0
    mean_ms: float = 0.0
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    max_ms: float = 0.0
    min_ms: float = 0.0


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summarise(values: list[float]) -> LatencyStats:
    if not values:
        return LatencyStats()
    return LatencyStats(
        count=len(values),
        mean_ms=round(sum(values) / len(values), 2),
        p50_ms=round(percentile(values, 0.50), 2),
        p90_ms=round(percentile(values, 0.90), 2),
        p95_ms=round(percentile(values, 0.95), 2),
        p99_ms=round(percentile(values, 0.99), 2),
        max_ms=round(max(values), 2),
        min_ms=round(min(values), 2),
    )


@dataclass
class TimeseriesPoint:
    second: int
    requests: int = 0
    errors: int = 0
    rps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0


def build_timeseries(results: list[Any]) -> list[TimeseriesPoint]:
    buckets: dict[int, list[Any]] = {}
    for result in results:
        buckets.setdefault(int(result.started_at), []).append(result)

    points: list[TimeseriesPoint] = []
    for second in sorted(buckets):
        group = buckets[second]
        latencies = [r.latency_ms for r in group]
        errors = sum(1 for r in group if not r.ok)
        points.append(
            TimeseriesPoint(
                second=second,
                requests=len(group),
                errors=errors,
                rps=float(len(group)),
                p50_ms=round(percentile(latencies, 0.50), 2),
                p95_ms=round(percentile(latencies, 0.95), 2),
                max_ms=round(max(latencies), 2),
            )
        )
    return points


def to_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    meta = report["meta"]
    latency = summary["latency"]
    ttft = summary["ttft"]

    lines: list[str] = []
    lines.append("# Load test report")
    lines.append("")
    lines.append(f"- Target: `{meta['base_url']}`")
    lines.append(f"- Mode: {'streaming' if meta['stream'] else 'blocking'}")
    lines.append(f"- Concurrency: {meta['concurrency']} (ramp {meta['ramp_s']}s)")
    lines.append(
        f"- Offered rate: {meta['rate']} rps (constant arrival)"
        if meta.get("rate")
        else "- Offered rate: closed loop"
    )
    lines.append(f"- Duration: {meta['duration_s']}s (warmup {meta['warmup_s']}s)")
    lines.append(f"- Started: {meta['started_at']}")
    lines.append("")
    lines.append("## Throughput and reliability")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Requests | {summary['requests']} |")
    lines.append(f"| Successful | {summary['successful']} |")
    lines.append(f"| Failed | {summary['failed']} |")
    lines.append(f"| Error rate | {summary['error_rate'] * 100:.2f}% |")
    lines.append(f"| Achieved RPS | {summary['achieved_rps']:.2f} |")
    lines.append(f"| Mean latency | {latency['mean_ms']} ms |")
    lines.append(
        f"| p50 / p95 / p99 latency | {latency['p50_ms']} / {latency['p95_ms']} / {latency['p99_ms']} ms |"
    )
    lines.append(f"| p50 / p95 TTFT | {ttft['p50_ms']} / {ttft['p95_ms']} ms |")
    tpot = summary.get("tpot", {})
    if tpot.get("count"):
        lines.append(f"| p50 / p95 inter-token latency | {tpot['p50_ms']} / {tpot['p95_ms']} ms |")
    lines.append(f"| Mean output rate | {summary['output_tokens_per_second']:.1f} tok/s |")
    lines.append("")

    if summary["status_codes"]:
        lines.append("## Status codes")
        lines.append("")
        lines.append("| Status | Count |")
        lines.append("| --- | --- |")
        for code, count in sorted(summary["status_codes"].items()):
            lines.append(f"| {code} | {count} |")
        lines.append("")

    if summary["errors"]:
        lines.append("## Error breakdown")
        lines.append("")
        lines.append("| Error | Count |")
        lines.append("| --- | --- |")
        for name, count in sorted(summary["errors"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {name} | {count} |")
        lines.append("")

    if summary["upstreams"]:
        lines.append("## Routing")
        lines.append("")
        lines.append("| Upstream | Count |")
        lines.append("| --- | --- |")
        for name, count in sorted(summary["upstreams"].items()):
            lines.append(f"| {name} | {count} |")
        lines.append("")

    cache = summary.get("cache", {})
    if cache:
        lines.append("## Cache")
        lines.append("")
        lines.append(f"- Hits: {cache.get('hits', 0)}")
        lines.append(f"- Misses: {cache.get('misses', 0)}")
        lines.append(f"- Hit ratio: {cache.get('hit_ratio', 0) * 100:.1f}%")
        lines.append("")

    if meta.get("chaos"):
        lines.append("## Chaos injection")
        lines.append("")
        for key, value in meta["chaos"].items():
            lines.append(f"- {key}: {value}")
        lines.append("")

    server = report.get("server_status")
    if server:
        lines.append("## Gateway state at end of run")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(server, indent=2))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)

