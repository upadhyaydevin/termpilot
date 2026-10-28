# TermPilot

Inline command suggestions for Zsh, ranked from your own shell history, all
on your machine.

As you type, TermPilot shows the most likely completion as dimmed ghost text.
Press **→** (or **Ctrl-E**) to accept it into the command line. It is never
run for you; you still press Enter.

```
$ git ch█eckout -b feature/search-ranking      ← dimmed ghost text
$ kgp█  → kubectl get pods -n staging           ← fuzzy match, shown in full
```

**What makes it different**

- **Ranks, not just recalls.** Suggestions combine match quality (prefix,
  token prefix such as `g ch ma`, or fuzzy such as `kgp`), recency, frequency,
  and how often you ran the command *in the current directory*. Every
  suggestion can be explained: `termpilot suggest --explain "git ch"`.
- **Local and private.** No network access, no telemetry. History is read,
  never modified, and the index is a private (`0600`) SQLite file you can
  delete at any time.
- **Never executes anything.** Suggestions are display-only until you accept
  them, and even then they only reach the command line. This is enforced by
  design and by end-to-end tests that drive a real zsh.
- **Measured, not claimed.** A reproducible synthetic benchmark is included;
  see [Performance](#performance).

## Features

- Zsh inline ghost-text suggestions, fetched asynchronously so typing never
  waits.
- Accept with Right arrow, Ctrl-F, Ctrl-E, or End (cursor at end of line).
  End works where it is bound to `end-of-line`, as macOS's default
  `/etc/zshrc` does. Turn suggestions off and on per shell with
  `termpilot_disable` / `termpilot_enable`.
- Robust parser for zsh history: EXTENDED_HISTORY timestamps, multi-line
  commands, zsh's "metafied" non-ASCII encoding, malformed lines.
- SQLite index with incremental refresh after each command, recovery from a
  corrupt index, and safe concurrent use from many shells.
- Optional per-directory context recorded by a pure-zsh hook.
- CLI: `index`, `suggest`, `doctor`, `stats`, `purge`, `init zsh`,
  `install-zsh` / `uninstall-zsh`, `paths`, `benchmark`.
- No runtime dependencies beyond the Python standard library.

## Limitations

- Tested on **macOS with zsh 5.9**. zsh ≥ 5.3 is required. Linux should work
  but is untested.
- Do not load it alongside `zsh-autosuggestions`; both draw ghost text in the
  same place.
- Ghost text shows up ~35 ms after a keystroke, mostly Python start-up time.
  It never blocks typing.
- Fuzzy matching only searches the most recent and frequent commands (plus
  everything sharing the typed prefix) to keep latency flat as history
  grows.
- Directory context starts from when you enable the integration. zsh history
  itself has no directory information.

More detail: [docs/design-decisions.md](docs/design-decisions.md#known-limitations).

## Requirements

- macOS (Linux untested), zsh 5.3+ (5.9 tested)
- Python 3.11+

## Installation

TermPilot is not on PyPI. Install it from a clone into its own virtual
environment:

```bash
git clone https://github.com/upadhyaydevin/termpilot.git
cd termpilot
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/termpilot index          # import your existing history (one-off)
```

With [pipx](https://pipx.pypa.io) you can run `pipx install .` instead, which
puts `termpilot` on your PATH.

### Enable it in zsh (opt-in)

TermPilot never edits your shell config unless you ask. Choose one:

**A. Add the snippet yourself (recommended).** Print it:

```bash
.venv/bin/termpilot init zsh
```

and paste the output at the end of `~/.zshrc`. It looks like:

```zsh
# >>> termpilot >>>
TERMPILOT_BIN='/path/to/termpilot/.venv/bin/termpilot'
source '/path/to/termpilot/src/termpilot/shell/termpilot.zsh'
# <<< termpilot <<<
```

**B. Let the installer do it.** It shows the block, asks for confirmation,
backs up `.zshrc` (as `.zshrc.termpilot-backup-<timestamp>`), and appends
only that marked block. Symlinked dotfiles stay symlinks.

```bash
.venv/bin/termpilot install-zsh
```

Open a new terminal, or run `exec zsh`. Then run `termpilot doctor` to check
the setup.

**Try it without touching your config:** `scripts/demo.sh --live` opens a
throwaway zsh with a synthetic history and the widget loaded. Set
`TERMPILOT_BIN=.venv/bin/termpilot` if `termpilot` isn't on your PATH.

## Usage

In the shell:

| Key / command | Effect |
|---|---|
| type normally | the best suggestion appears as dimmed text |
| **→**, **Ctrl-F**, **Ctrl-E**, **End** at end of line | accept it into the command line (not run) |
| keep typing / Backspace | suggestion updates or disappears |
| `termpilot_disable` / `termpilot_enable` | turn suggestions off/on in this shell |

From the CLI (handy for debugging the ranking):

```console
$ termpilot suggest "git ch"
git checkout main
git checkout -b feature/search-ranking
git commit -m 'Add ranking weights'

$ termpilot suggest --explain "git ch"
0.597  git checkout main    [prefix; match=1.00 recency=0.05 frequency=0.24 context=0.00]
0.583  git checkout -b feature/search-ranking    [prefix; match=1.00 recency=0.05 frequency=0.15 context=0.00]
0.207  git commit -m 'Add ranking weights'    [fuzzy; match=0.32 recency=0.05 frequency=0.15 context=0.00]

$ termpilot suggest kgp
kubectl get pods -n staging

$ termpilot suggest --format json -n 3 "docker"     # machine-readable
$ termpilot index                                     # refresh now (runs automatically after each command)
$ termpilot index --rebuild                           # start the index from scratch
$ termpilot stats                                     # counts only, never command text
$ termpilot doctor                                    # check config, history, index, zsh setup
```

The output above is from `scripts/demo.sh`, which uses a synthetic history.

Exit codes: `0` success, `1` no suggestion, `2` usage error, `3` config
error, `4` history/index problem (or a failed `doctor` check), `70`
unexpected error (rerun with `--debug` for a traceback).

## Configuration and data locations

Everything is optional. Settings live in
`~/.config/termpilot/config.toml` (or `$XDG_CONFIG_HOME/termpilot/config.toml`):

```toml
history_path = "~/.zsh_history"   # default: $HISTFILE, else ~/.zsh_history
# data_dir = "~/Library/Application Support/termpilot"
context = true                     # record the directory each command runs in

[ranking]
min_input_length = 2       # shorter input gets no suggestions
max_suggestions = 5        # results from `termpilot suggest`
weight_match = 0.55
weight_recency = 0.20
weight_frequency = 0.15
weight_context = 0.10
min_match = 0.30           # relevance gate: weaker matches are never shown
recency_half_life_days = 7.0
```

**Precedence** (highest first): CLI flags (`--history`, `--data-dir`, `-n`),
then environment variables (`TERMPILOT_CONFIG`, `TERMPILOT_HISTORY`,
`TERMPILOT_DATA_DIR`), then the config file, then defaults. Unknown keys and
wrong types are reported as errors rather than ignored.

**Shell settings** (set before the `source` line): `TERMPILOT_BIN`,
`TERMPILOT_HIGHLIGHT` (default `fg=8`), `TERMPILOT_CONTEXT=0` to stop
directory logging, `TERMPILOT_AUTO_INDEX=0` to stop refreshing after each
command.

**Files** (`termpilot paths` prints them):

| File | Contents |
|---|---|
| `~/Library/Application Support/termpilot/index.db` | distinct commands with counts and last-used time; per-directory counts |
| `~/Library/Application Support/termpilot/context.log` | `time<TAB>directory<TAB>command` per command run, written by the zsh hook |
| your zsh history | read only, never modified |

On Linux the data directory is `$XDG_DATA_HOME/termpilot`.

## Privacy and security

- **No network.** TermPilot makes no network requests and has no telemetry.
- **Read-only history.** Your history file is opened read-only and never
  written, moved or deleted.
- **Private files.** The data directory is `0700`, and the index and context
  log are `0600`.
- **Minimal storage.** The index keeps each distinct command with a count and
  a last-used time, and nothing else (no exit codes, durations or ordering).
- **Never executes or evaluates history.** The zsh script contains no
  `eval`. Typed text reaches Python as a single argument. Suggestions go into
  zsh's display-only `POSTDISPLAY`, and into the command line only when you
  accept them.
- **Untrusted text is filtered.** History entries containing terminal
  control characters are skipped, so a suggestion cannot inject escape
  sequences into your terminal. All SQL is parameterized.
- **Quiet by default.** `doctor`, `stats` and `index` print counts and paths,
  never command text. Only `suggest` prints commands, for the input you give
  it.
- **Respects hidden commands.** With `HIST_IGNORE_SPACE` set, commands typed
  with a leading space are not written to the context log.
- **Delete everything** TermPilot stored with `termpilot purge`. It does not
  touch your history.

## Architecture

```
src/termpilot/
├── history.py      parse zsh history + context log (read-only, streaming)
├── ranking.py      pure, deterministic scoring and ordering
├── storage.py      SQLite index: incremental refresh, bounded retrieval
├── config.py       defaults < TOML file < environment < CLI flags
├── cli.py          commands, exit codes, error messages
├── integration.py  .zshrc snippet, install / uninstall
├── benchmark.py    synthetic datasets and timing
└── shell/termpilot.zsh   ZLE widget: async fetch, ghost text, accept keys,
                          preexec context log, precmd background refresh
```

The zsh widget runs `termpilot suggest --format inline` asynchronously on
each edit. That command does a read-only SQLite lookup, ranks in Python, and
prints one line. After each command, `precmd` starts `termpilot index
--quiet` in the background, which parses only the bytes appended to your
history and context log since the last run.

[docs/design-decisions.md](docs/design-decisions.md) explains the
trade-offs: the ranking formula, SQLite as a rebuildable cache, how rewrites
of the history file are detected, why there is no daemon, and more.

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                   # all tests (includes real-zsh PTY tests)
.venv/bin/pytest -m "not zsh"      # skip the PTY tests
.venv/bin/black --check src tests && .venv/bin/flake8 src tests && .venv/bin/mypy
```

All tests use synthetic data in temporary directories. An autouse fixture
redirects `HOME`, `HISTFILE` and TermPilot's paths, so your real history is
never read. `tests/test_zsh_integration.py` starts a real interactive zsh in
a pseudo-terminal and checks that ghost text appears, accepting works,
**Enter runs only the typed text**, history navigation and editing are
unaffected, and a crashing or hanging backend never blocks the shell.

## Performance

Reproduce with `termpilot benchmark`. It generates seeded synthetic
histories, so no real data is involved. Full method and tables are in
[docs/benchmarks.md](docs/benchmarks.md).

Single run on an Apple M4, macOS 26.5, CPython 3.12.4, SQLite 3.45.3, zsh
5.9. 200 queries per size (lookup + rank) and 30 process runs (zsh
end-to-end). Values are p50 / p95 in milliseconds:

| History entries | distinct | lookup + rank | zsh end-to-end | full index | incremental refresh (+100) |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 254 | 0.44 / 0.65 | 32.80 / 40.21 | 2.56 | 0.42 |
| 10,000 | 2,060 | 2.81 / 4.87 | 35.00 / 36.84 | 17.43 | 0.41 |
| 100,000 | 18,073 | 3.79 / 7.87 | 36.58 / 42.66 | 167.50 | 0.61 |

- Query time stays nearly flat from 10k to 100k because retrieval is bounded.
- End-to-end time is dominated by ~31 ms of Python start-up and imports. It
  is asynchronous, so it never delays keystrokes.
- Profiling-driven fixes are documented in
  [docs/benchmarks.md](docs/benchmarks.md#optimizations-driven-by-profiling).
  One of them was a regex that could backtrack for minutes on adversarial
  input.

## Troubleshooting

Start with `termpilot doctor`.

| Symptom | Fix |
|---|---|
| No ghost text at all | `termpilot doctor`; check that `termpilot index` has run and that `TERMPILOT_BIN` points at the executable (the snippet sets it) |
| `termpilot: 'termpilot' not found` at shell start | set `TERMPILOT_BIN` to the full path before the `source` line, or rerun `termpilot init zsh` |
| `no index at …; run termpilot index` | run `termpilot index` once; after that it refreshes automatically |
| `cannot read index … --rebuild` | `termpilot index --rebuild` (a corrupt file is moved aside automatically) |
| History file not found | set `history_path` in the config, or pass `--history`; zsh's `$HISTFILE` is used by default |
| Suggestions are old | zsh writes history at exit unless `INC_APPEND_HISTORY` or `SHARE_HISTORY` is set; the context log covers the current session meanwhile |
| Right arrow behaves oddly | another plugin may also wrap `forward-char`; `termpilot_disable` restores the original |
| Ghost text hard to see | set e.g. `TERMPILOT_HIGHLIGHT='fg=244'` before sourcing |

## Uninstall

```bash
termpilot uninstall-zsh     # removes only the marked block (backs up .zshrc first)
termpilot purge             # deletes index.db and context.log, not your history
```

Then delete the cloned folder (or `pipx uninstall termpilot`). If you added
the snippet by hand, delete the lines between `# >>> termpilot >>>` and
`# <<< termpilot <<<` in `~/.zshrc`.

## License

MIT. See [LICENSE](LICENSE).
