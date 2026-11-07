from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

REGISTRY = CollectorRegistry(auto_describe=True)


def histogram(
    name: str,
    documentation: str,
    labelnames: list[str] = [],
    buckets: list[float] | None = None,
) -> Histogram:
    return Histogram(
        name,
        documentation,
        labelnames=labelnames,
        buckets=buckets,
        registry=REGISTRY,
    )


def counter(
    name: str,
    documentation: str,
    labelnames: list[str] = [],
) -> Counter:
    return Counter(name, documentation, labelnames=labelnames, registry=REGISTRY)


def gauge(
    name: str,
    documentation: str,
    labelnames: list[str] = [],
) -> Gauge:
    return Gauge(name, documentation, labelnames=labelnames, registry=REGISTRY)


def render_latest() -> bytes:
    return generate_latest(REGISTRY)

