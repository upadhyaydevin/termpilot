from __future__ import annotations

from pathlib import Path

import pytest

from termpilot.benchmark import (
    format_report,
    generate_commands,
    generate_queries,
    percentile,
    run_benchmark,
)


def test_generator_is_deterministic_and_synthetic() -> None:
    a = generate_commands(500, seed=3)
    b = generate_commands(500, seed=3)
    assert a == b
    assert a != generate_commands(500, seed=4)
    assert len(a) == 500
    assert [ts for ts, _ in a] == sorted(ts for ts, _ in a)
    # Hosts are reserved example domains; nothing resembles real data.
    hosts = {w for _, c in a for w in c.split() if "host-" in w}
    assert all(".example.com" in h for h in hosts)


def test_generator_has_realistic_repetition() -> None:
    entries = generate_commands(5000)
    distinct = len({c for _, c in entries})
    assert 0.05 < distinct / len(entries) < 0.6


def test_queries_mix() -> None:
    entries = generate_commands(1000)
    qs = generate_queries(entries, 400)
    assert len(qs) == 400
    commands = [c for _, c in entries]
    prefixes = sum(any(c.startswith(q) for c in commands) for q in qs)
    assert 0.6 < prefixes / len(qs) < 0.95
    assert any(q.startswith("zq") for q in qs)


@pytest.mark.parametrize(
    ("pct", "expected"),
    [(50, 5.0), (95, 10.0), (100, 10.0), (0, 1.0), (10, 1.0), (11, 2.0)],
)
def test_percentile_nearest_rank(pct: float, expected: float) -> None:
    assert percentile([float(x) for x in range(10, 0, -1)], pct) == expected


def test_percentile_empty() -> None:
    with pytest.raises(ValueError):
        percentile([], 50)


def test_small_end_to_end_run(tmp_path: Path) -> None:
    report = run_benchmark([300], queries=10, process_runs=2)
    (result,) = report["results"]  # type: ignore[misc]
    assert result["entries"] == 300
    phases = result["phases"]
    for name in ("parse", "index_full", "index_incr", "lookup", "rank", "baseline", "cli"):
        assert phases[name]["n"] > 0
        assert phases[name]["p50_ms"] <= phases[name]["p95_ms"] <= phases[name]["max_ms"]
    text = format_report(report)
    assert "300 history entries" in text and "| lookup |" in text
    # The benchmark must not create anything in the configured data dir.
    assert not (tmp_path / "data").exists()
