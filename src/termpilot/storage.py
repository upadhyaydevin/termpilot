"""SQLite index of command usage.

The database is a *derived cache*: everything in it can be rebuilt from the
zsh history file and TermPilot's context log. That keeps three things simple:

* Schema changes: an older schema is dropped and rebuilt, never migrated.
* Corruption: a corrupt file is moved aside and rebuilt.
* Privacy: deleting the database loses nothing that is not already on disk.

Only the minimum is stored: each distinct command with its use count and
last-use time, plus per-directory counts from the context log. No timing,
exit status or ordering of commands is kept.

Incremental refresh: for each source file we remember how many bytes we have
consumed plus a hash of the first and last few KB of that consumed region.
If those still match, only the bytes appended since are parsed. If not (zsh
trimmed or deduplicated its history, or the file was replaced) the source is
re-imported from scratch.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from termpilot.history import ParseStats, check_readable, iter_context_log, iter_history
from termpilot.ranking import Candidate

SCHEMA_VERSION = 1

# Candidate retrieval limits. Ranking runs in Python, so these bound the
# per-keystroke cost regardless of history size (see docs/design-decisions.md).
PREFIX_LIMIT = 1000  # commands that literally start with the typed text
RECENT_POOL = 2000  # most recently used commands, for fuzzy matching
FREQUENT_POOL = 500  # most frequently used commands, for fuzzy matching
CONTEXT_POOL = 300  # most recent commands run in the current directory

_ANCHOR_BYTES = 4096

_SCHEMA = """
CREATE TABLE commands (
    command    TEXT PRIMARY KEY,
    hist_count INTEGER NOT NULL DEFAULT 0,  -- occurrences in the history file
    log_count  INTEGER NOT NULL DEFAULT 0,  -- occurrences in the context log
    last_used  INTEGER                      -- epoch seconds, NULL if unknown
);
CREATE INDEX commands_last_used ON commands(last_used);
CREATE INDEX commands_hist_count ON commands(hist_count);

CREATE TABLE dir_usage (
    directory TEXT NOT NULL,
    command   TEXT NOT NULL,
    count     INTEGER NOT NULL,
    last_used INTEGER NOT NULL,
    PRIMARY KEY (directory, command)
) WITHOUT ROWID;

CREATE TABLE sources (
    name        TEXT PRIMARY KEY,  -- 'history' or 'context'
    path        TEXT NOT NULL,
    offset      INTEGER NOT NULL,
    head_hash   TEXT NOT NULL,
    tail_hash   TEXT NOT NULL
);
"""

_UPSERT_HISTORY = """
INSERT INTO commands (command, hist_count, last_used) VALUES (?, ?, ?)
ON CONFLICT (command) DO UPDATE SET
    hist_count = hist_count + excluded.hist_count,
    last_used = NULLIF(max(coalesce(last_used, 0), coalesce(excluded.last_used, 0)), 0)
"""

_UPSERT_LOG = """
INSERT INTO commands (command, log_count, last_used) VALUES (?, ?, ?)
ON CONFLICT (command) DO UPDATE SET
    log_count = log_count + excluded.log_count,
    last_used = NULLIF(max(coalesce(last_used, 0), coalesce(excluded.last_used, 0)), 0)
"""

_UPSERT_DIR = """
INSERT INTO dir_usage (directory, command, count, last_used) VALUES (?, ?, ?, ?)
ON CONFLICT (directory, command) DO UPDATE SET
    count = count + excluded.count,
    last_used = max(last_used, excluded.last_used)
"""

# The history file and the context log record the *same* executions (the log
# only since the zsh integration was enabled), so counts are combined with
# max() rather than summed to avoid double counting.
_COUNT = "max(hist_count, log_count)"


class StorageError(Exception):
    """The index is missing, locked, corrupt, or from an incompatible version."""


@dataclass(slots=True)
class SourceResult:
    mode: str = "unchanged"  # 'unchanged' | 'incremental' | 'full' | 'missing' | 'disabled'
    entries: int = 0
    stats: ParseStats = field(default_factory=ParseStats)


@dataclass(slots=True)
class RefreshResult:
    history: SourceResult = field(default_factory=SourceResult)
    context: SourceResult = field(default_factory=SourceResult)
    recovered_from: Path | None = None  # set if a corrupt DB was moved aside


# --- Opening ---------------------------------------------------------------


def _secure_create(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _create_schema(conn: sqlite3.Connection) -> None:
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        conn.execute(f'DROP TABLE "{name}"')
    # Not executescript(): it COMMITs first, which would break atomicity.
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _connect(path: Path, *, readonly: bool, timeout: float) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    else:
        conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    # Touch the file header so corruption is detected here, not mid-query.
    _schema_version(conn)
    return conn


def open_readonly(path: Path, *, timeout: float = 0.2) -> sqlite3.Connection:
    """Open the index for queries. Never creates or modifies anything."""
    if not path.is_file():
        raise StorageError(f"no index at {path}; run `termpilot index` first")
    try:
        conn = _connect(path, readonly=True, timeout=timeout)
        version = _schema_version(conn)
    except sqlite3.DatabaseError as exc:
        raise StorageError(
            f"cannot read index {path} ({exc}); run `termpilot index --rebuild`"
        ) from None
    if version != SCHEMA_VERSION:
        conn.close()
        raise StorageError(
            f"index {path} has schema v{version}, expected v{SCHEMA_VERSION}; "
            "run `termpilot index --rebuild`"
        )
    return conn


def _move_aside(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    path.replace(target)
    for suffix in ("-wal", "-shm"):
        extra = path.with_name(path.name + suffix)
        if extra.exists():
            extra.unlink()
    return target


def open_writable(path: Path, *, timeout: float = 2.0) -> tuple[sqlite3.Connection, Path | None]:
    """Open (creating or repairing if needed) the index for writing.

    Returns the connection and, if the existing file was corrupt, where it was
    moved to so the caller can tell the user.
    """
    recovered: Path | None = None
    try:
        _secure_create(path)
    except OSError as exc:
        raise StorageError(f"cannot create index at {path}: {exc.strerror}") from None
    try:
        conn = _connect(path, readonly=False, timeout=timeout)
        version = _schema_version(conn)
    except sqlite3.DatabaseError as exc:
        if "locked" in str(exc):
            raise StorageError(f"index {path} is locked by another process") from None
        recovered = _move_aside(path)
        _secure_create(path)
        conn = _connect(path, readonly=False, timeout=timeout)
        version = 0
    if version > SCHEMA_VERSION:
        conn.close()
        raise StorageError(
            f"index {path} was written by a newer TermPilot (schema v{version}); "
            "upgrade TermPilot or delete the index"
        )
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        if version != SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            _create_schema(conn)
            conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        conn.close()
        raise StorageError(f"cannot initialise index {path}: {exc}") from None
    return conn, recovered


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """``BEGIN IMMEDIATE`` so concurrent refreshes from several shells serialise."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        raise StorageError(f"index is busy ({exc}); try again") from None
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- Refreshing ------------------------------------------------------------


def _anchor_hashes(path: Path, offset: int) -> tuple[str, str]:
    """Hash the first and last few KB of the already-consumed region."""
    with path.open("rb") as handle:
        head = handle.read(min(offset, _ANCHOR_BYTES))
        tail_start = max(0, offset - _ANCHOR_BYTES)
        handle.seek(tail_start)
        tail = handle.read(offset - tail_start)
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()


def _plan(conn: sqlite3.Connection, name: str, path: Path) -> tuple[str, int]:
    """Decide whether a source needs a full or incremental import."""
    row = conn.execute(
        "SELECT path, offset, head_hash, tail_hash FROM sources WHERE name = ?", (name,)
    ).fetchone()
    if row is None or row[0] != str(path):
        return "full", 0
    _, offset, head_hash, tail_hash = row
    size = path.stat().st_size
    if size < offset or _anchor_hashes(path, offset) != (head_hash, tail_hash):
        return "full", 0
    if size == offset:
        return "unchanged", offset
    return "incremental", offset


def _save_source(conn: sqlite3.Connection, name: str, path: Path, offset: int) -> None:
    head, tail = _anchor_hashes(path, offset)
    conn.execute(
        "INSERT OR REPLACE INTO sources (name, path, offset, head_hash, tail_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, str(path), offset, head, tail),
    )


def _import_history(conn: sqlite3.Connection, path: Path, result: SourceResult) -> None:
    check_readable(path, "history file")
    mode, offset = _plan(conn, "history", path)
    result.mode = mode
    if mode == "unchanged":
        return
    if mode == "full":
        conn.execute("UPDATE commands SET hist_count = 0")
    counts: dict[str, list[int | None]] = {}
    for entry in iter_history(
        path, start_offset=offset, include_incomplete=False, stats=result.stats
    ):
        slot = counts.setdefault(entry.command, [0, None])
        slot[0] = (slot[0] or 0) + 1
        if entry.timestamp is not None:
            slot[1] = max(slot[1] or 0, entry.timestamp)
        result.entries += 1
    conn.executemany(_UPSERT_HISTORY, ((cmd, n, ts) for cmd, (n, ts) in counts.items()))
    if mode == "full":
        conn.execute("DELETE FROM commands WHERE hist_count = 0 AND log_count = 0")
    _save_source(conn, "history", path, result.stats.end_offset)


def _import_context(conn: sqlite3.Connection, path: Path, result: SourceResult) -> None:
    mode, offset = _plan(conn, "context", path)
    result.mode = mode
    if mode == "unchanged":
        return
    if mode == "full":
        conn.execute("UPDATE commands SET log_count = 0")
        conn.execute("DELETE FROM dir_usage")
    cmd_counts: dict[str, list[int]] = {}
    dir_counts: dict[tuple[str, str], list[int]] = {}
    for entry in iter_context_log(path, start_offset=offset, stats=result.stats):
        c = cmd_counts.setdefault(entry.command, [0, 0])
        c[0] += 1
        c[1] = max(c[1], entry.timestamp)
        d = dir_counts.setdefault((entry.directory, entry.command), [0, 0])
        d[0] += 1
        d[1] = max(d[1], entry.timestamp)
        result.entries += 1
    conn.executemany(_UPSERT_LOG, ((cmd, n, ts) for cmd, (n, ts) in cmd_counts.items()))
    conn.executemany(_UPSERT_DIR, ((d, cmd, n, ts) for (d, cmd), (n, ts) in dir_counts.items()))
    if mode == "full":
        conn.execute("DELETE FROM commands WHERE hist_count = 0 AND log_count = 0")
    _save_source(conn, "context", path, result.stats.end_offset)


def refresh(
    conn: sqlite3.Connection,
    history_path: Path,
    context_path: Path | None,
) -> RefreshResult:
    """Bring the index up to date with the history file and context log.

    Runs in one transaction: a crash or kill leaves the previous state intact.
    A missing context log is normal (integration not enabled yet); a missing
    history file raises ``HistoryError`` from the parser.
    """
    result = RefreshResult()
    with transaction(conn):
        _import_history(conn, history_path, result.history)
        if context_path is None:
            result.context.mode = "disabled"
        elif context_path.is_file():
            _import_context(conn, context_path, result.context)
        else:
            result.context.mode = "missing"
    return result


def rebuild(conn: sqlite3.Connection) -> None:
    """Drop everything; the next refresh re-imports all sources."""
    with transaction(conn):
        _create_schema(conn)


# --- Querying --------------------------------------------------------------


def _prefix_upper_bound(prefix: str) -> str:
    # U+10FFFF is the largest code point, and its UTF-8 encoding sorts after
    # every other, so [prefix, prefix + U+10FFFF) covers all extensions under
    # SQLite's default BINARY (memcmp of UTF-8) collation.
    return prefix + "\U0010ffff"


def fetch_candidates(
    conn: sqlite3.Connection, query: str, cwd: str | None = None
) -> list[Candidate]:
    """Retrieve a bounded candidate set for ``query`` (see module docstring).

    Results may contain the same command more than once (e.g. found by both
    prefix and recency); callers should use the de-duplicated set.
    """
    prefix = query.lstrip()
    rows: dict[str, tuple[int, int | None]] = {}

    def collect(sql: str, params: tuple[object, ...]) -> None:
        for command, count, last_used in conn.execute(sql, params):
            rows[command] = (count, last_used)

    cols = f"SELECT command, {_COUNT}, last_used FROM commands"
    if prefix:
        collect(
            f"{cols} WHERE command >= ? AND command < ? ORDER BY last_used DESC LIMIT ?",
            (prefix, _prefix_upper_bound(prefix), PREFIX_LIMIT),
        )
    collect(f"{cols} ORDER BY last_used DESC LIMIT ?", (RECENT_POOL,))
    collect(f"{cols} ORDER BY hist_count DESC LIMIT ?", (FREQUENT_POOL,))

    dir_counts: dict[str, int] = {}
    if cwd:
        for command, dir_count, count, last_used in conn.execute(
            "SELECT d.command, d.count, max(c.hist_count, c.log_count), c.last_used "
            "FROM dir_usage d JOIN commands c ON c.command = d.command "
            "WHERE d.directory = ? ORDER BY d.last_used DESC LIMIT ?",
            (cwd, CONTEXT_POOL),
        ):
            dir_counts[command] = dir_count
            rows[command] = (count, last_used)

    return [
        Candidate(cmd, count=count, last_used=last, dir_count=dir_counts.get(cmd, 0))
        for cmd, (count, last) in rows.items()
    ]


@dataclass(frozen=True, slots=True)
class IndexStats:
    commands: int
    directories: int
    history_offset: int | None
    context_offset: int | None


def index_stats(conn: sqlite3.Connection) -> IndexStats:
    """Aggregate counts only; never returns command text."""
    commands = conn.execute("SELECT count(*) FROM commands").fetchone()[0]
    directories = conn.execute("SELECT count(DISTINCT directory) FROM dir_usage").fetchone()[0]
    offsets = dict(conn.execute("SELECT name, offset FROM sources").fetchall())
    return IndexStats(commands, directories, offsets.get("history"), offsets.get("context"))
