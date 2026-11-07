"""Print a one-line summary per scenario report."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    paths = argv[1:] or sorted(str(p) for p in Path("reports").glob("scenario-*.json"))
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        summary = report["summary"]
        latency = summary["latency"]
        ttft = summary["ttft"]
        tpot = summary.get("tpot", {})
        cache = summary["cache"]
        print(
            f"{path.stem.replace('scenario-', ''):12s} "
            f"rps={summary['achieved_rps']:6.2f} "
            f"err={summary['error_rate'] * 100:5.2f}% "
            f"p50={latency['p50_ms']:8.1f}ms p95={latency['p95_ms']:8.1f}ms "
            f"ttft50={ttft['p50_ms']:7.1f}ms "
            f"tpot50={tpot.get('p50_ms', 0):6.2f}ms "
            f"cache={cache['hit_ratio'] * 100:5.1f}% "
            f"status={summary['status_codes']} "
            f"up={summary['upstreams']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

