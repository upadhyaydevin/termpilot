"""Reproducible latency benchmark on synthetic history.

Nothing here reads real shell history. A seeded generator writes a zsh
history file into a temporary directory, and every phase runs against that.

Phases (per dataset size):
  parse          stream-parse the whole history file
  index_full     build a fresh SQLite index from it
  index_incr     refresh after appending 100 new entries
  lookup         storage.fetch_candidates() per query (SQLite retrieval)
  rank           ranking.rank() on those candidates (pure Python)
  baseline       rank() over *every* distinct command, no index (Stage 1 design)
  cli            `python -m termpilot suggest --format inline` as a subprocess
  zsh            the same command run from zsh via $(...), timed with
                 $EPOCHREALTIME, as the widget does (includes zsh fork cost)

Percentiles use the nearest-rank method on the raw samples.
"""

from __future__ import annotations

import json
import os
import platform
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from termpilot.history import iter_history
from termpilot.ranking import Candidate, rank
from termpilot.storage import fetch_candidates, open_readonly, open_writable, refresh

NOW = 1_760_000_000  # fixed "current time" so ranking is reproducible
YEAR = 365 * 86400

_BRANCHES = ["main", "develop", "release", "hotfix"]
_WORDS = [
    "api", "auth", "cache", "config", "data", "deploy", "docs", "events", "feature",
    "frontend", "infra", "jobs", "logs", "metrics", "search", "server", "tests", "users",
]  # fmt: skip
_TEMPLATES = [
    "git status",
    "git checkout {branch}",
    "git checkout -b {word}/{word2}-{n}",
    "git commit -m '{word} {word2} fix {n}'",
    "git push origin {branch}",
    "git log --oneline -n {n}",
    "cd ~/src/{word}-{word2}",
    "ls -la {word}/",
    "docker run --rm -it {word}-{word2}:{n}",
    "docker compose -f {word}.yml up -d",
    "kubectl get pods -n {word}-{n}",
    "kubectl logs -f deploy/{word}-{word2} -n {word}",
    "npm run {word}",
    "npm install --save-dev {word}-{word2}",
    "python -m pytest tests/test_{word}.py -k {word2}",
    "python scripts/{word}_{word2}.py --limit {n}",
    "make {word}",
    "vim src/{word}/{word2}.py",
    "ssh deploy@host-{n}.example.com",
    "curl -s https://host-{n}.example.com/{word}/{word2}",
    "grep -rn '{word}' src/{word2}",
    "cargo build --release -p {word}-{word2}",
    "terraform plan -var-file={word}.tfvars",
    "brew upgrade {word}",
]


def _fill(template: str, rnd: random.Random) -> str:
    return template.format(
        branch=rnd.choice(_BRANCHES),
        word=rnd.choice(_WORDS),
        word2=rnd.choice(_WORDS),
        n=rnd.randint(1, 400),
    )


def generate_commands(n: int, seed: int = 42) -> list[tuple[int, str]]:
    """Return ``n`` synthetic ``(timestamp, command)`` pairs, oldest first.

    Popularity is Zipf-like (weight 1/rank) over a pool of distinct commands
    sized at 40% of ``n``, so a few commands repeat often and most are rare.
    """
    rnd = random.Random(seed)
    pool_size = max(1, int(n * 0.4))
    pool: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(pool) < pool_size and attempts < pool_size * 20:
        attempts += 1
        cmd = _fill(rnd.choice(_TEMPLATES), rnd)
        if cmd not in seen:
            seen.add(cmd)
            pool.append(cmd)
    weights = [1.0 / (i + 1) for i in range(len(pool))]
    commands = rnd.choices(pool, weights=weights, k=n)
    start = NOW - YEAR
    step = YEAR / max(1, n)
    return [(int(start + i * step), cmd) for i, cmd in enumerate(commands)]


def write_history(path: Path, entries: list[tuple[int, str]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for ts, cmd in entries:
            handle.write(f": {ts}:0;{cmd}\n")


def generate_queries(entries: list[tuple[int, str]], count: int, seed: int = 7) -> list[str]:
    """~75% prefixes, ~20% fuzzy abbreviations, ~5% inputs that match nothing."""
    rnd = random.Random(seed)
    queries: list[str] = []
    for _ in range(count):
        cmd = rnd.choice(entries)[1]
        roll = rnd.random()
        if roll < 0.75:
            queries.append(cmd[: rnd.randint(2, min(len(cmd), 15))])
        elif roll < 0.95:
            # First letter of each word: "git checkout main" -> "gcm".
            abbrev = "".join(word[0] for word in cmd.split()[:4])
            queries.append(abbrev if len(abbrev) >= 2 else cmd[:3])
        else:
            queries.append("zq" + "".join(rnd.choice("xjvq") for _ in range(4)))
    return queries


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile (no interpolation)."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    k = max(0, min(len(ordered) - 1, int(-(-pct * len(ordered) // 100)) - 1))
    return ordered[k]


@dataclass
class Timing:
    n: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    @classmethod
    def from_seconds(cls, samples: list[float]) -> Timing:
        ms = [s * 1000 for s in samples]
        return cls(len(ms), percentile(ms, 50), percentile(ms, 95), max(ms))


@dataclass
class SizeResult:
    entries: int
    distinct_commands: int
    history_bytes: int
    db_bytes: int
    phases: dict[str, Timing] = field(default_factory=dict)


def _time(fn: Callable[[], object], repeat: int) -> list[float]:
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return samples


def _subprocess_env(data_dir: Path, workdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        TERMPILOT_DATA_DIR=str(data_dir),
        TERMPILOT_CONFIG=str(workdir / "no-config.toml"),
        HOME=str(workdir),
    )
    return env


def environment() -> dict[str, str]:
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "sqlite": sqlite3.sqlite_version,
        "cpu": platform.processor() or "unknown",
        "zsh": "not found",
    }
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5, check=True,
            )  # fmt: skip
            info["cpu"] = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    zsh = shutil.which("zsh")
    if zsh:
        out = subprocess.run([zsh, "--version"], capture_output=True, text=True, timeout=5)
        info["zsh"] = out.stdout.strip()
    return info


_ZSH_TIMER = r"""
zmodload zsh/datetime
local python=$1; shift
local q t0 t1
for q in "$@"; do
  t0=$EPOCHREALTIME
  : "$("$python" -m termpilot suggest --format inline -- "$q" 2>/dev/null)"
  t1=$EPOCHREALTIME
  print -r -- $(( t1 - t0 ))
done
"""


def run_size(
    size: int,
    workdir: Path,
    *,
    queries: int,
    process_runs: int,
    seed: int,
    log: Callable[[str], None],
) -> SizeResult:
    data_dir = workdir / f"data-{size}"
    db_path = data_dir / "index.db"
    hist = workdir / f"history-{size}"
    entries = generate_commands(size, seed)
    write_history(hist, entries)
    qs = generate_queries(entries, queries, seed + 1)
    phases: dict[str, Timing] = {}

    log(f"[{size}] parse")
    phases["parse"] = Timing.from_seconds(_time(lambda: sum(1 for _ in iter_history(hist)), 3))

    log(f"[{size}] index_full")

    def build() -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        conn, _ = open_writable(db_path)
        refresh(conn, hist, None)
        conn.close()

    phases["index_full"] = Timing.from_seconds(_time(build, 3))

    log(f"[{size}] index_incr")
    extra = generate_commands(100, seed + 2)
    incr: list[float] = []
    for _ in range(5):
        with hist.open("a", encoding="utf-8") as handle:
            for ts, cmd in extra:
                handle.write(f": {ts + YEAR}:0;{cmd}\n")
        conn, _ = open_writable(db_path)
        start = time.perf_counter()
        refresh(conn, hist, None)
        incr.append(time.perf_counter() - start)
        conn.close()
    phases["index_incr"] = Timing.from_seconds(incr)

    log(f"[{size}] lookup + rank ({len(qs)} queries)")
    conn = open_readonly(db_path)
    lookup: list[float] = []
    ranking: list[float] = []
    for q in qs:
        t0 = time.perf_counter()
        cands = fetch_candidates(conn, q)
        t1 = time.perf_counter()
        rank(q, cands, now=NOW)
        t2 = time.perf_counter()
        lookup.append(t1 - t0)
        ranking.append(t2 - t1)
    distinct = conn.execute("SELECT count(*) FROM commands").fetchone()[0]
    all_cands = [
        Candidate(cmd, count=max(h, lg), last_used=last)
        for cmd, h, lg, last in conn.execute(
            "SELECT command, hist_count, log_count, last_used FROM commands"
        )
    ]
    conn.close()
    phases["lookup"] = Timing.from_seconds(lookup)
    phases["rank"] = Timing.from_seconds(ranking)
    phases["lookup+rank"] = Timing.from_seconds([a + b for a, b in zip(lookup, ranking)])

    base_qs = qs[: max(5, min(len(qs), 30))]
    log(f"[{size}] baseline ({len(base_qs)} queries over all {distinct} commands)")
    baseline: list[float] = []
    for q in base_qs:
        start = time.perf_counter()
        rank(q, all_cands, now=NOW)
        baseline.append(time.perf_counter() - start)
    phases["baseline"] = Timing.from_seconds(baseline)

    if process_runs > 0:
        env = _subprocess_env(data_dir, workdir)
        proc_qs = [qs[i % len(qs)] for i in range(process_runs)]
        log(f"[{size}] cli subprocess ({process_runs} runs)")
        argv = [sys.executable, "-m", "termpilot", "suggest", "--format", "inline", "--"]
        cli: list[float] = []
        for q in proc_qs:
            start = time.perf_counter()
            subprocess.run([*argv, q], env=env, capture_output=True, check=False)
            cli.append(time.perf_counter() - start)
        phases["cli"] = Timing.from_seconds(cli)

        zsh = shutil.which("zsh")
        if zsh:
            log(f"[{size}] zsh end-to-end ({process_runs} runs)")
            out = subprocess.run(
                [zsh, "-f", "-c", _ZSH_TIMER, "zsh", sys.executable, *proc_qs],
                env=env, capture_output=True, text=True, check=True,
            )  # fmt: skip
            phases["zsh"] = Timing.from_seconds([float(x) for x in out.stdout.split()])

    return SizeResult(
        entries=size,
        distinct_commands=distinct,
        history_bytes=hist.stat().st_size,
        db_bytes=db_path.stat().st_size,
        phases=phases,
    )


def run_benchmark(
    sizes: list[int],
    *,
    queries: int = 200,
    process_runs: int = 30,
    seed: int = 42,
    log: Callable[[str], None] = lambda _msg: None,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="termpilot-bench-") as tmp:
        workdir = Path(tmp)
        results = [
            run_size(size, workdir, queries=queries, process_runs=process_runs, seed=seed, log=log)
            for size in sizes
        ]
    return {
        "environment": environment(),
        "settings": {
            "sizes": sizes,
            "queries": queries,
            "process_runs": process_runs,
            "seed": seed,
        },
        "results": [asdict(r) for r in results],
    }


def format_report(report: dict[str, object]) -> str:
    env = report["environment"]
    settings = report["settings"]
    assert isinstance(env, dict) and isinstance(settings, dict)
    lines = ["## Environment", ""]
    lines += [f"- {k}: {v}" for k, v in env.items()]
    lines += [
        "",
        f"Settings: seed={settings['seed']}, {settings['queries']} queries per size, "
        f"{settings['process_runs']} subprocess runs per size.",
        "",
    ]
    results = report["results"]
    assert isinstance(results, list)
    for res in results:
        lines += [
            f"## {res['entries']:,} history entries "
            f"({res['distinct_commands']:,} distinct; history {res['history_bytes']:,} B, "
            f"index {res['db_bytes']:,} B)",
            "",
            "| phase | n | p50 ms | p95 ms | max ms |",
            "|---|---:|---:|---:|---:|",
        ]
        for name, t in res["phases"].items():
            cells = f"{t['p50_ms']:.2f} | {t['p95_ms']:.2f} | {t['max_ms']:.2f}"
            lines.append(f"| {name} | {t['n']} | {cells} |")
        lines.append("")
    return "\n".join(lines)


def save_json(report: dict[str, object], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
