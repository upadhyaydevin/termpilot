from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from termpilot.history import (
    HistoryError,
    ParseStats,
    escape_field,
    iter_context_log,
    iter_history,
    normalize_command,
    read_history,
    unmetafy,
)

WriteHistory = Callable[[bytes], Path]


def metafy(data: bytes) -> bytes:
    """Encode like zsh does for bytes 0x83-0x9f (used to build fixtures)."""
    out = bytearray()
    for b in data:
        if 0x83 <= b <= 0x9F:
            out += bytes([0x83, b ^ 0x20])
        else:
            out.append(b)
    return bytes(out)


def test_parses_sample_fixture(sample_history: Path) -> None:
    entries, stats = read_history(sample_history)
    commands = [e.command for e in entries]
    assert commands == [
        "git status",
        "git checkout main",
        "ls -la",
        "docker compose up -d",
        "plain-command --without-timestamp",
        "git status",
        "echo first line\nsecond line",
        "cd ~/projects/demo",
        "git checkout -b feature/search",
        "python -m pytest -q",
        "echo café",
        "git status",
    ]
    assert entries[0].timestamp == 1700000000
    assert entries[4].timestamp is None
    assert stats.blank == 1
    assert stats.entries == len(entries)


def test_extended_format_without_elapsed(write_history: WriteHistory) -> None:
    path = write_history(b": 1700000000;make test\n")
    entries, _ = read_history(path)
    assert [(e.command, e.timestamp) for e in entries] == [("make test", 1700000000)]


def test_null_command_is_not_mistaken_for_metadata(write_history: WriteHistory) -> None:
    path = write_history(b": just-a-noop\n")
    entries, _ = read_history(path)
    assert [e.command for e in entries] == [": just-a-noop"]


def test_unmetafy_roundtrip() -> None:
    original = "echo ă ŝ ☃".encode()
    assert 0x83 in original  # "ă" is C4 83 in UTF-8
    assert unmetafy(metafy(original)) == original


def test_metafied_history_is_decoded(write_history: WriteHistory) -> None:
    path = write_history(b": 1:0;" + metafy("echo ăbc".encode()) + b"\n")
    entries, _ = read_history(path)
    assert [e.command for e in entries] == ["echo ăbc"]


def test_non_metafied_utf8_still_decodes(write_history: WriteHistory) -> None:
    # A file written by some other tool, containing a raw 0x83 byte.
    path = write_history("echo ăx\n".encode())
    entries, _ = read_history(path)
    assert [e.command for e in entries] == ["echo ăx"]


def test_invalid_utf8_is_skipped_not_mangled(write_history: WriteHistory) -> None:
    path = write_history(b"echo \xff\xfe\nls\n")
    entries, stats = read_history(path)
    assert [e.command for e in entries] == ["ls"]
    assert stats.undecodable == 1


def test_control_characters_are_rejected(write_history: WriteHistory) -> None:
    path = write_history(b"printf '\x1b[31mred'\nls\n")
    entries, stats = read_history(path)
    assert [e.command for e in entries] == ["ls"]
    assert stats.unsafe == 1


def test_overlong_commands_are_skipped(write_history: WriteHistory) -> None:
    path = write_history(b"echo " + b"x" * 5000 + b"\nls\n")
    entries, stats = read_history(path)
    assert [e.command for e in entries] == ["ls"]
    assert stats.too_long == 1


def test_crlf_and_blank_lines(write_history: WriteHistory) -> None:
    path = write_history(b"\n\n   \nls -l\r\n")
    entries, stats = read_history(path)
    assert [e.command for e in entries] == ["ls -l"]
    assert stats.blank == 3


def test_incomplete_tail_handling(write_history: WriteHistory) -> None:
    path = write_history(b"ls\npartial-wri")
    assert [e.command for e in iter_history(path)] == ["ls", "partial-wri"]
    assert [e.command for e in iter_history(path, include_incomplete=False)] == ["ls"]


def test_dangling_continuation_at_eof(write_history: WriteHistory) -> None:
    path = write_history(b"ls\necho a\\\n")
    assert [e.command for e in iter_history(path, include_incomplete=False)] == ["ls"]


def test_resume_from_offset(write_history: WriteHistory) -> None:
    path = write_history(b"one\ntwo\\\ncontinued\nthree\n")
    entries = list(iter_history(path))
    assert [e.command for e in entries] == ["one", "two\ncontinued", "three"]
    resumed = list(iter_history(path, start_offset=entries[0].end_offset))
    assert [e.command for e in resumed] == ["two\ncontinued", "three"]
    assert entries[-1].end_offset == path.stat().st_size


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  ls  ", "ls"),
        ("", None),
        ("   \t ", None),
        ("echo a\\ ", "echo a\\ "),  # escaped trailing space is meaningful
        ("git commit -m 'x  y'", "git commit -m 'x  y'"),  # inner spacing untouched
    ],
)
def test_normalize_command(raw: str, expected: str | None) -> None:
    assert normalize_command(raw) == expected


def test_missing_file_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(HistoryError, match="not found"):
        read_history(tmp_path / "nope")


def test_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(HistoryError, match="not a regular file"):
        read_history(tmp_path)


def test_unreadable_file_has_clear_error(write_history: WriteHistory) -> None:
    import os

    path = write_history(b"ls\n")
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):  # e.g. running as root
            pytest.skip("cannot make file unreadable in this environment")
        with pytest.raises(HistoryError, match="permission denied"):
            read_history(path)
    finally:
        path.chmod(0o600)


def test_parser_never_modifies_file(sample_history: Path) -> None:
    before = sample_history.read_bytes()
    mtime = sample_history.stat().st_mtime_ns
    read_history(sample_history)
    assert sample_history.read_bytes() == before
    assert sample_history.stat().st_mtime_ns == mtime


def test_large_file_streams(write_history: WriteHistory) -> None:
    lines = b"".join(
        b": %d:0;cmd-%d --flag\n" % (1_700_000_000 + i, i % 500) for i in range(50_000)
    )
    path = write_history(lines)
    stats = ParseStats()
    count = sum(1 for _ in iter_history(path, stats=stats))
    assert count == 50_000 == stats.entries


def test_context_log_parsing(tmp_path: Path) -> None:
    log = tmp_path / "context.log"
    cmd = "printf 'a\\tb'\necho done"
    log.write_text(
        f"1700000000\t/Users/x/proj\tmake test\n"
        f"1700000001\t/Users/x/we\\tird\t{escape_field(cmd)}\n"
        "garbage line\n"
        "1700000002\trelative/dir\tls\n"
        "1700000003\t/tmp\tbad \\q escape\n"
        "1700000004\t/tmp\tpartial",
        encoding="utf-8",
    )
    stats = ParseStats()
    entries = list(iter_context_log(log, stats=stats))
    assert [(e.directory, e.command) for e in entries] == [
        ("/Users/x/proj", "make test"),
        ("/Users/x/we\tird", cmd),
    ]
    assert stats.malformed == 3
    # The partial last line is left for the next read.
    assert entries[-1].end_offset < log.stat().st_size
