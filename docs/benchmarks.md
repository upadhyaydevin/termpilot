# Benchmarks

All numbers on this page were produced by `termpilot benchmark` on synthetic
data. No real shell history was used. To reproduce:

```bash
termpilot benchmark --json results.json --markdown results.md
```

## Method

- **Dataset**: `termpilot.benchmark.generate_commands(n, seed=42)` fills 24
  command templates (git, docker, kubectl, npm, make, ssh to
  `host-N.example.com`, ...) with random words and numbers. A pool of
  distinct commands (40% of `n`) is sampled with Zipf-like weights (1/rank),
  so a few commands repeat often and most are rare. Timestamps are spread
  evenly over one year. The file is written in zsh EXTENDED_HISTORY format.
- **Queries**: 200 per size, sampled from the dataset: ~75% prefixes of 2-15
  characters, ~20% first-letter abbreviations (`gcm`), ~5% strings that match
  nothing.
- **Phases** (all timed with `time.perf_counter`, except `zsh`):
  - `parse`: stream-parse the whole history file (3 runs).
  - `index_full`: build a fresh SQLite index (3 runs).
  - `index_incr`: refresh after appending 100 entries (5 runs).
  - `lookup`: `storage.fetch_candidates()` per query (SQLite retrieval only).
  - `rank`: `ranking.rank()` on those candidates (pure Python).
  - `baseline`: `rank()` over *every* distinct command with no index, with
    candidates already in memory (30 queries). The real Stage 1 design also
    re-parsed the history file per query, which adds the `parse` time on top.
  - `cli`: `python -m termpilot suggest --format inline` as a subprocess
    (30 runs): process start + imports + lookup + rank.
  - `zsh`: the same command run from `zsh -f` via `$(...)`, timed with
    `$EPOCHREALTIME` (30 runs). This is the path the widget uses, including
    zsh's fork.
- **Statistics**: nearest-rank p50/p95 on raw samples, plus the maximum. Each
  table is a single benchmark run on one machine, so p95 values for 30-sample
  phases are rough and sensitive to background load.

## Optimizations, driven by profiling

1. **Deferred imports (Stage 4).** `python -X importtime` showed module
   imports were the largest part of each keystroke's process, ahead of
   Python startup and the query. Imports used only by rarely-run commands
   (`tomllib`, `hashlib`, `json`, `platform`, `shutil`, the installer) moved
   into the functions that use them. Measured from zsh at 100k entries
   (n=32): p50 47.4 ms -> 43.1 ms.
2. **Pre-filter before validation (Stage 5).** `cProfile` over 200 queries at
   100k entries showed `ranking._sanitize` (defensive validation of every
   candidate) taking 1.26 s of 2.39 s (53%). Running the cheap C-level
   subsequence check on the raw command string first means only plausible
   candidates are validated.
3. **Linear-time fuzzy pre-filter (Stage 5, also a correctness fix).** The
   original `a.*?b.*?c` regex backtracked catastrophically: `"-"*12 + "x"`
   against a 400-character run of dashes ran for over two minutes without
   finishing. Atomic groups `(?>.*?a)(?>.*?b)` with `re.match` give the same
   answer in linear time (0.15 ms for that input). A randomized test checks
   it against a plain-Python subsequence check.

Before/after for 2 and 3, 100,000 entries, p50 / p95 in ms:

| phase | before | after |
|---|---:|---:|
| lookup | 2.20 / 4.43 | 2.19 / 4.59 |
| rank | 3.61 / 5.37 | 1.80 / 3.86 |
| lookup+rank | 5.81 / 9.12 | 3.79 / 7.87 |
| baseline | 25.19 / 32.87 | 7.03 / 18.75 |

## Interpreting the results

- Query cost (`lookup+rank`) grows slowly with history size because
  retrieval is bounded: at most 1,000 prefix matches plus the 2,000 most
  recent and 500 most frequent commands (`storage.py`).
- End-to-end latency is dominated by a roughly fixed ~31 ms of Python
  process start and imports. At 1,000 entries the query is under 1 ms, yet
  the subprocess takes ~33 ms. zsh adds little on top of that.
- Requests are asynchronous (`zle -F`), so this latency delays when ghost
  text *appears*. It never blocks typing.
- A persistent daemon could remove most of the startup cost but adds a
  process to manage, a socket and a lifecycle. With measured end-to-end
  latency around 35-40 ms and non-blocking input, that complexity was not
  judged worth it (see `design-decisions.md`).

## Results (after optimizations)

### Environment

- platform: macOS-26.5.2-arm64-arm-64bit
- machine: arm64
- python: CPython 3.12.4
- sqlite: 3.45.3
- cpu: Apple M4
- zsh: zsh 5.9 (arm64-apple-darwin25.0)

Settings: seed=42, 200 queries per size, 30 subprocess runs per size.

### 1,000 history entries (254 distinct; history 67,022 B, index 57,344 B)

| phase | n | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|
| parse | 3 | 1.16 | 1.20 | 1.20 |
| index_full | 3 | 2.56 | 4.96 | 4.96 |
| index_incr | 5 | 0.42 | 0.45 | 0.45 |
| lookup | 200 | 0.29 | 0.31 | 0.39 |
| rank | 200 | 0.15 | 0.35 | 0.56 |
| lookup+rank | 200 | 0.44 | 0.65 | 0.85 |
| baseline | 30 | 0.11 | 0.38 | 0.38 |
| cli | 30 | 33.72 | 55.14 | 57.54 |
| zsh | 30 | 32.80 | 40.21 | 40.56 |

### 10,000 history entries (2,060 distinct; history 476,542 B, index 282,624 B)

| phase | n | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|
| parse | 3 | 11.09 | 11.45 | 11.45 |
| index_full | 3 | 17.43 | 17.58 | 17.58 |
| index_incr | 5 | 0.41 | 0.55 | 0.55 |
| lookup | 200 | 1.83 | 2.00 | 3.27 |
| rank | 200 | 0.93 | 2.99 | 4.67 |
| lookup+rank | 200 | 2.81 | 4.87 | 6.51 |
| baseline | 30 | 1.08 | 2.78 | 3.09 |
| cli | 30 | 35.54 | 37.55 | 37.91 |
| zsh | 30 | 35.00 | 36.84 | 37.45 |

### 100,000 history entries (18,073 distinct; history 4,699,386 B, index 2,260,992 B)

| phase | n | p50 ms | p95 ms | max ms |
|---|---:|---:|---:|---:|
| parse | 3 | 112.86 | 114.63 | 114.63 |
| index_full | 3 | 167.50 | 169.93 | 169.93 |
| index_incr | 5 | 0.61 | 0.78 | 0.78 |
| lookup | 200 | 2.19 | 4.59 | 10.52 |
| rank | 200 | 1.80 | 3.86 | 7.61 |
| lookup+rank | 200 | 3.79 | 7.87 | 14.19 |
| baseline | 30 | 7.03 | 18.75 | 18.85 |
| cli | 30 | 39.05 | 54.69 | 65.90 |
| zsh | 30 | 36.58 | 42.66 | 47.96 |

