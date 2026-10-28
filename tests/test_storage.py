from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from termpilot.history import HistoryError, escape_field
from termpilot.storage import (
    SCHEMA_VERSION,
    StorageError,
    fetch_candidates,
    index_stats,
    open_readonly,
    open_writable,
    rebuild,
    refresh,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "index.db"


def hist_line(ts: int, cmd: str) -> bytes:
    return f": {ts}:0;{cmd}\n".encode()


def counts(db: Path) -> dict[str, tuple[int, int, int | None]]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT command, hist_count, log_count, last_used FROM commands")
    result = {r[0]: (r[1], r[2], r[3]) for r in rows}
    conn.close()
    return result


def index(db: Path, history: Path, context: Path | None = None):  # type: ignore[no-untyped-def]
    conn, _ = open_writable(db)
    try:
        return refresh(conn, history, context)
    finally:
        conn.close()


def test_initial_import_aggregates(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(100, "ls") + hist_line(200, "git status") + hist_line(300, "ls"))
    result = index(db, hist)
    assert result.history.mode == "full" and result.history.entries == 3
    assert counts(db) == {"ls": (2, 0, 300), "git status": (1, 0, 200)}


def test_files_are_private(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls"))
    index(db, hist)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700


def test_incremental_refresh_only_reads_appended(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(100, "ls"))
    index(db, hist)
    assert index(db, hist).history.mode == "unchanged"
    with hist.open("ab") as f:
        f.write(hist_line(200, "ls") + hist_line(250, "make"))
    result = index(db, hist)
    assert (result.history.mode, result.history.entries) == ("incremental", 2)
    assert counts(db) == {"ls": (2, 0, 200), "make": (1, 0, 250)}


def test_partial_line_is_imported_once_complete(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(100, "ls") + b": 200:0;git sta")
    index(db, hist)
    assert counts(db) == {"ls": (1, 0, 100)}
    with hist.open("ab") as f:
        f.write(b"tus\n")
    index(db, hist)
    assert counts(db) == {"ls": (1, 0, 100), "git status": (1, 0, 200)}


def test_trailing_skipped_lines_are_not_reread(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls") + b"\n\n")
    index(db, hist)
    assert index(db, hist).history.mode == "unchanged"


def test_rewritten_history_triggers_full_reimport(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(100, "old-cmd") + hist_line(200, "ls"))
    index(db, hist)
    # zsh trimmed the oldest entry and appended a new one: head changed.
    hist.write_bytes(hist_line(200, "ls") + hist_line(300, "new-cmd") + hist_line(400, "ls"))
    result = index(db, hist)
    assert result.history.mode == "full"
    assert counts(db) == {"ls": (2, 0, 400), "new-cmd": (1, 0, 300)}


def test_rewrite_with_same_head_detected_by_tail_hash(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(100, "keep") + hist_line(200, "dup") + hist_line(300, "x"))
    index(db, hist)
    # Deduplication removed a middle entry; file is longer but content shifted.
    hist.write_bytes(hist_line(100, "keep") + hist_line(300, "x") + hist_line(400, "y" * 30))
    assert index(db, hist).history.mode == "full"
    assert set(counts(db)) == {"keep", "x", "y" * 30}


def test_history_file_never_modified(db: Path, sample_history: Path) -> None:
    before = sample_history.read_bytes()
    index(db, sample_history)
    assert sample_history.read_bytes() == before


def test_missing_history_raises_and_keeps_index(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls"))
    index(db, hist)
    hist.unlink()
    with pytest.raises(HistoryError, match="not found"):
        index(db, hist)
    assert counts(db) == {"ls": (1, 0, 1)}


def test_context_log_import(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(100, "make test") + hist_line(110, "make test"))
    log = tmp_path / "context.log"
    log.write_text(
        "120\t/proj/a\tmake test\n130\t/proj/b\tmake lint\n"
        f"140\t/proj/a\t{escape_field('echo a' + chr(10) + 'b')}\n",
        encoding="utf-8",
    )
    result = index(db, hist, log)
    assert result.context.mode == "full" and result.context.entries == 3
    c = counts(db)
    # History and log record the same runs: counts are combined with max().
    assert c["make test"] == (2, 1, 120)
    assert c["make lint"] == (0, 1, 130)

    conn = open_readonly(db)
    cands = {x.command: x for x in fetch_candidates(conn, "make", cwd="/proj/a")}
    conn.close()
    assert cands["make test"].count == 2 and cands["make test"].dir_count == 1
    assert cands["make lint"].dir_count == 0


def test_context_disabled_and_missing(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls"))
    assert index(db, hist, None).context.mode == "disabled"
    assert index(db, hist, tmp_path / "nope.log").context.mode == "missing"


def test_rebuild_reimports(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls"))
    index(db, hist)
    conn, _ = open_writable(db)
    rebuild(conn)
    assert index_stats(conn).commands == 0
    assert refresh(conn, hist, None).history.mode == "full"
    assert index_stats(conn).commands == 1
    conn.close()


def test_fetch_prefix_and_pools(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(
        hist_line(1, "git status")
        + hist_line(2, "git stash")
        + hist_line(3, "gitk")
        + hist_line(4, "go")
    )
    index(db, hist)
    conn = open_readonly(db)
    commands = {c.command for c in fetch_candidates(conn, "git st")}
    conn.close()
    # Prefix matches are always included; the pools add everything here (tiny DB).
    assert {"git status", "git stash"} <= commands


def test_prefix_range_handles_unicode_and_special_chars(db: Path, tmp_path: Path) -> None:
    import termpilot.storage as storage

    hist = tmp_path / "h"
    cmds = ["echo ☃ snow", "echo ☃☃", "echo %_like_wildcards%", "echo 𝄞 clef"]
    hist.write_bytes(b"".join(hist_line(i, c) for i, c in enumerate(cmds, 1)))
    index(db, hist)
    conn = open_readonly(db)
    # Disable the recency/frequency pools so only the prefix query contributes.
    old = (storage.RECENT_POOL, storage.FREQUENT_POOL)
    storage.RECENT_POOL = storage.FREQUENT_POOL = 0
    try:
        assert {c.command for c in fetch_candidates(conn, "echo ☃")} == {"echo ☃ snow", "echo ☃☃"}
        assert {c.command for c in fetch_candidates(conn, "echo %")} == {"echo %_like_wildcards%"}
        assert {c.command for c in fetch_candidates(conn, "echo 𝄞")} == {"echo 𝄞 clef"}
    finally:
        storage.RECENT_POOL, storage.FREQUENT_POOL = old
        conn.close()


def test_readonly_never_creates(db: Path) -> None:
    with pytest.raises(StorageError, match="termpilot index"):
        open_readonly(db)
    assert not db.exists()


def test_corrupt_database_is_moved_aside(db: Path, tmp_path: Path) -> None:
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is definitely not sqlite" * 100)
    with pytest.raises(StorageError, match="--rebuild"):
        open_readonly(db)
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls"))
    conn, recovered = open_writable(db)
    assert recovered is not None and recovered.exists()
    refresh(conn, hist, None)
    conn.close()
    assert counts(db) == {"ls": (1, 0, 1)}


def test_old_schema_is_rebuilt_newer_schema_is_refused(db: Path, tmp_path: Path) -> None:
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE legacy (x)")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()
    conn, _ = open_writable(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "legacy" not in tables
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StorageError, match="newer"):
        open_writable(db)
    with pytest.raises(StorageError, match="schema"):
        open_readonly(db)


def test_locked_database_reports_busy(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "ls"))
    index(db, hist)
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        conn, _ = open_writable(db, timeout=0.05)
        with pytest.raises(StorageError, match="busy"):
            refresh(conn, hist, None)
        conn.close()
        # Readers are not blocked by a writer (WAL mode).
        reader = open_readonly(db)
        assert index_stats(reader).commands == 1
        reader.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_stats_do_not_expose_commands(db: Path, tmp_path: Path) -> None:
    hist = tmp_path / "h"
    hist.write_bytes(hist_line(1, "export SECRET=hunter2"))
    index(db, hist)
    conn = open_readonly(db)
    stats = index_stats(conn)
    conn.close()
    assert "hunter2" not in repr(stats)
    assert stats.commands == 1
