# Design decisions

This document explains the main trade-offs in TermPilot and why each choice
was made. Measurements quoted here come from `termpilot benchmark` (see
[benchmarks.md](benchmarks.md)).

## Architecture at a glance

```
 typing in zsh                          after each command
 ─────────────                          ──────────────────
 line-pre-redraw hook                   preexec: append (time, cwd, cmd)
   │  (buffer changed?)                          to context.log (pure zsh)
   ▼                                    precmd:  `termpilot index --quiet &!`
 exec {fd}< <(termpilot suggest ...)               │
   │  zle -F fd  ── non-blocking ──┐               ▼
   ▼                               │    storage.refresh()
 cli.py ─► storage.fetch_candidates│      ├─ history.iter_history()  (zsh history)
   │       (SQLite, read-only)     │      └─ history.iter_context_log()
   ▼                               │               │
 ranking.rank()  (pure Python)     │               ▼
   │                               │         index.db (SQLite, WAL)
   ▼                               │
 stdout ─► zle handler ─► POSTDISPLAY (ghost text, display only)
                          Right/End/Ctrl-E ─► BUFFER (still not executed)
```

| Module | Responsibility | Knows about |
|---|---|---|
| `history.py` | Parse zsh history and the context log | files only |
| `ranking.py` | Score and order candidates | nothing else (pure) |
| `storage.py` | SQLite index: refresh and candidate retrieval | history, ranking types |
| `config.py` | Defaults, TOML file, env, CLI precedence | ranking config |
| `cli.py` | Commands, exit codes, user-facing errors | everything |
| `integration.py` | `.zshrc` snippet, install and uninstall | files only |
| `shell/termpilot.zsh` | ZLE widget, hooks, context logging | the `termpilot` CLI |
| `benchmark.py` | Synthetic data and timing | history, storage, ranking |

## History parsing

- **Follow zsh's own reader.** zsh writes a backslash before each embedded
  newline and, when reading, turns a trailing `\` back into a newline. The
  parser does exactly that, so multi-line commands round-trip.
- **Unmetafy before decoding.** zsh escapes bytes 0x83-0x9f (which appear
  inside ordinary UTF-8, e.g. `ă` is `C4 83`) as `0x83, byte^0x20`. Skipping
  this step corrupts non-ASCII commands. If the unmetafied bytes are not
  valid UTF-8, the raw bytes are tried, which covers files written by other
  tools.
- **Skip rather than repair.** Entries that cannot be decoded, contain
  control characters (which could inject terminal escape sequences when
  displayed), or exceed 4096 characters are counted and skipped. Command
  text is never rewritten, except for trimming surrounding whitespace where
  that cannot change meaning (`echo a\ ` keeps its escaped space).
- **Streaming, read-only.** Files are read line by line in binary mode and
  never written. `ParseStats.end_offset` records the end of the last
  *complete* line so incremental reads resume exactly there. A line still
  being written by zsh is left for the next refresh.

## Ranking

All weights and thresholds live in `ranking.RankingConfig` and can be
overridden in the `[ranking]` table of the config file.

```
score = 0.55·match + 0.20·recency + 0.15·frequency + 0.10·context
```

| Feature | Definition |
|---|---|
| match | 1.0 exact prefix, 0.9 case-insensitive prefix, 0.75 token prefix (`g ch ma` → `git checkout main`), up to 0.6 fuzzy |
| recency | `0.5 ** (age / 7 days)` |
| frequency | `log(1+count) / log(1+100)`, capped at 1 |
| context | `log(1+runs in this directory) / log(1+20)`, capped at 1 |

- **Relevance gate.** Candidates with `match < 0.30` are dropped before
  scoring. Without this gate, a command run 10,000 times would be suggested
  for unrelated input.
- **Prefix first.** Fuzzy matches are capped at 0.6, so a prefix match has a
  0.22+ head start from the match term alone. A fuzzy match can still win
  when it is much more recent and frequent.
- **Fuzzy scoring** is a transparent subsequence scorer: one point per
  matched character, plus one if it follows the previous match, plus one if
  it starts a word. It then scales by coverage and tries up to 8 start
  positions. **No dependency was added.** Ranking takes ~1.8 ms p50 at 100k
  entries, so a compiled library such as RapidFuzz would not change
  perceived latency, and its edit-distance scores are a poorer fit for
  abbreviations like `gcm`.
- **Determinism.** `now` is passed in rather than read from the clock. Ties
  break on most recent use, then count, then alphabetical order. Scores are
  rounded to 9 decimal places so float noise cannot decide a tie.
- **Short or empty input** (fewer than 2 non-space characters by default)
  returns nothing, so the history is never dumped. Input over 512
  characters (a paste) also returns nothing.
- **Linear-time pre-filter.** A regex checks that the query is a
  subsequence of the candidate before any Python scoring runs. It uses
  atomic groups (`(?>.*?a)(?>.*?b)…`) with `re.match`. The naive
  `a.*?b.*?c` form backtracked catastrophically on inputs like
  `"-"*12 + "x"` (minutes per candidate). Leftmost choices are always
  optimal for subsequence matching, so the atomic version is exact.

## Storage: SQLite as a derived cache

**Why SQLite.** At 100k history entries, re-parsing the history per keystroke
costs ~113 ms before any ranking. An index makes each query a bounded
lookup (~2 ms). SQLite is in the standard library, gives atomic transactions
(safe even if a keystroke's process is killed mid-write), and in WAL mode
lets a background refresh run without blocking readers.

**Why "derived cache".** Everything in `index.db` can be rebuilt from the
history file and the context log. That makes several things simple:

- *Schema changes*: `PRAGMA user_version` is checked on open. An older
  schema is dropped and rebuilt, never migrated. A newer schema is refused,
  with a message to upgrade.
- *Corruption*: a file SQLite cannot read is moved aside to
  `index.db.corrupt-<timestamp>` and rebuilt. Read-only callers just report
  the error and suggest `termpilot index --rebuild`.
- *Privacy*: `termpilot purge` loses nothing that is not already on disk
  elsewhere.

**What is stored.** Only `commands(command, hist_count, log_count,
last_used)`, `dir_usage(directory, command, count, last_used)`, and per-source
bookkeeping. There is no ordering, duration or exit status. Files are
created `0600` in a `0700` directory. Every query is parameterized.

**Incremental refresh.** For each source the index stores the byte offset it
has consumed, plus SHA-256 hashes of the first and last 4 KB of that region.
On refresh:

- size unchanged and hashes match → nothing to do
- grown and hashes match → parse only the appended bytes
- anything else → the file was rewritten, so re-import that source

The tail hash catches rewrites that keep the head intact (for example
`HIST_IGNORE_ALL_DUPS` removing an older duplicate). zsh rewrites the file
when trimming to `SAVEHIST`, which the head hash catches. Measured:
appending 100 entries to a 100k-entry history refreshes in ~0.6 ms.

**Counting across sources.** The history file and the context log record the
*same* executions, so a command's count is `max(hist_count, log_count)`
rather than the sum. That is an approximation: it undercounts if some runs
only reached one source. It never double-counts.

**Bounded retrieval.** Per query, `fetch_candidates` reads at most 1,000
commands with the typed prefix (an index range scan using
`[prefix, prefix + U+10FFFF)`, which is safe with `%`/`_` and any Unicode),
the 2,000 most recent commands, the 500 most frequent, and 300 from the
current directory. Ranking cost therefore stays roughly flat as history
grows. The trade-off is that a fuzzy-only match for a command that is
neither recent nor frequent will not be found.

## Directory context

zsh history does not record the working directory, so the integration adds
a tiny `preexec` hook, written in **pure zsh** with no Python per command. It
appends `epoch<TAB>cwd<TAB>command` to `context.log`, escaping `\`, newline
and tab. The file is created with `umask 077`. Commands with a leading space
are skipped when `HIST_IGNORE_SPACE` is set, matching what zsh itself would
not save. It can be turned off with `context = false` in the config or
`TERMPILOT_CONTEXT=0`.

The log grows without bound. Each line is a timestamp, a path and a
command, so at roughly 100 bytes per line, 10,000 commands is about 1 MB.
Rotation was left out to keep the hook trivial. `termpilot purge` deletes
the log.

## Zsh integration

- **Supported APIs only.** It uses `add-zle-hook-widget line-pre-redraw` to
  notice edits, `zle -F` on a process-substitution file descriptor for
  asynchronous results, `POSTDISPLAY` plus `region_highlight` for ghost
  text, and `add-zsh-hook` for preexec/precmd. There are no raw terminal
  escape hacks.
- **Asynchronous by design.** The request runs in a background process.
  Typing never waits for it. A new keystroke cancels the old request
  (killing it when `zsh/system` provides its PID), and a response is
  discarded if the line changed in the meantime. While a request is
  running, the previous suggestion stays visible if what you typed is still
  a prefix of it, so the display does not flicker.
- **Never executes.** The typed text is passed as one argv element after
  `--`. The suggestion is placed in `POSTDISPLAY`, which zle displays but
  never runs, or in `BUFFER` when you accept it. The script contains no
  `eval` (a unit test checks this). An end-to-end PTY test waits with a
  suggestion visible and asserts that its output never appears, then
  presses Enter and asserts that only the typed text ran. A deliberately
  broken script that auto-executed suggestions failed that test.
- **Accept keys wrap, not replace.** `forward-char`, `end-of-line`,
  `vi-forward-char` and `vi-end-of-line` are wrapped. If one of them was
  already a user widget (from another plugin), the wrapper calls it when
  there is no suggestion, and `termpilot_disable` restores it.
- **Stays out of the way.** Lines recalled with history or search widgets
  get no ghost text. Ghost text is cleared when the cursor leaves the end of
  the line and in `zle-line-finish`, so it never lingers in scrollback.
- **Fuzzy suggestions** cannot be shown as a continuation of what you typed,
  so they appear as `  → full command`. Accepting one replaces the line.

## Why there is no daemon

The keystroke path starts a Python process each time. Measured from zsh,
end-to-end latency is ~33-37 ms p50 for 1k-100k entries. About 31 ms of that
is interpreter start plus imports, which is the same at every size.

A long-running daemon would remove most of that. It would also need a
socket, a lifecycle (start, stop, crash recovery, upgrades), and another
process with access to your history. Because requests are asynchronous and
never block typing, ~35 ms only affects when the ghost text appears. That
is below a typical gap between keystrokes. The cheaper fix was taken
instead: imports not needed on the keystroke path are deferred (measured
47 → 43 ms at the time). This is the first thing to revisit if latency
becomes a problem on slower machines.

## Configuration

Precedence, highest first: CLI flags (`--history`, `--data-dir`, `-n`),
then `TERMPILOT_*` environment variables, then the TOML config file, then
defaults. `$HISTFILE` is treated as a *default*, because zsh sets it in
almost every shell and it should not silently override an explicit
`history_path` in the config. Unknown keys and wrong types are errors (exit
code 3) rather than being ignored, so a typo like `wieght_match` does not go
unnoticed.

## Testing strategy

- Every test uses synthetic fixtures under `tmp_path`. An autouse fixture
  points `HOME`, `HISTFILE`, the config path and the data dir at temporary
  locations, so no test can read real history.
- Time is injected (`now=`). Storage tests cover corruption, locking,
  schema versions, rewrites and partial lines.
- `tests/test_zsh_integration.py` runs a real `zsh -d -i` in a
  pseudo-terminal with an isolated `ZDOTDIR`. It types keys and asserts on
  what the terminal shows. It also covers a backend that crashes and one
  that hangs: the shell must stay responsive.
- The PTY tests were checked by mutation: disabling the integration failed 4
  tests, and making suggestions auto-execute failed the safety test.

## Known limitations

- Tested on macOS with zsh 5.9 only. The script requires zsh ≥ 5.3; Linux
  and older zsh versions are untested.
- Not compatible with `zsh-autosuggestions` in the same shell. Both use
  `POSTDISPLAY` and wrap the same widgets.
- Ghost text colour (`fg=8`) and multi-line suggestions are verified only as
  output in a PTY with `TERM=xterm-256color`, not visually in Terminal.app
  or iTerm2.
- vi-mode widgets are wrapped but not covered by automated tests.
- History entries without timestamps (plain format) get a recency of 0.
- `HISTORY_IGNORE` patterns are not applied to the context log.
