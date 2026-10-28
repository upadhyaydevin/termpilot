"""Read Zsh history files and TermPilot's directory-context log.

Everything here is read-only: files are opened in binary mode, never written,
and their contents are treated as untrusted text. Nothing parsed here is ever
executed.

Zsh history format (what zsh itself writes, see ``Src/hist.c``):

* Plain lines: ``ls -la``
* EXTENDED_HISTORY lines: ``: <start epoch>:<elapsed seconds>;<command>``
* A command containing newlines is written with a backslash before each
  embedded newline. When zsh reads the file it turns a trailing ``\\`` into a
  newline and keeps reading; we do the same.
* "Metafied" bytes: zsh escapes some bytes (notably 0x83 and 0x84-0x9f, which
  occur inside UTF-8 sequences such as ``ă``) as ``0x83`` followed by the byte
  XOR 0x20. We undo this before decoding UTF-8.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from termpilot.errors import TermPilotError

ZSH_META = 0x83

# Commands longer than this are almost always pasted scripts or heredocs;
# they make poor suggestions and bloat the index.
MAX_COMMAND_LENGTH = 4096

_EXTENDED_RE = re.compile(rb"^: ?(\d{1,12})(?::(\d{1,12}))?;(.*)\Z", re.DOTALL)
# C0 control characters (except tab and newline) and DEL. A history entry that
# contains these could emit terminal escape sequences when displayed.
_UNSAFE_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class HistoryError(TermPilotError):
    """A history or context-log file could not be read."""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    command: str
    timestamp: int | None
    # Byte offset just past this entry; safe to resume reading from.
    end_offset: int


@dataclass(slots=True)
class ParseStats:
    entries: int = 0
    blank: int = 0
    undecodable: int = 0
    unsafe: int = 0
    too_long: int = 0
    malformed: int = 0
    # Byte offset just past the last *complete* line consumed, including lines
    # that were skipped. Resuming from here never re-reads or loses data.
    end_offset: int = 0

    @property
    def skipped(self) -> int:
        return self.blank + self.undecodable + self.unsafe + self.too_long + self.malformed


@dataclass(frozen=True, slots=True)
class ContextEntry:
    command: str
    directory: str
    timestamp: int
    end_offset: int


def unmetafy(raw: bytes) -> bytes:
    """Undo zsh's metafication (``0x83 X`` -> ``X ^ 0x20``)."""
    if ZSH_META not in raw:
        return raw
    out = bytearray()
    it = iter(raw)
    for byte in it:
        if byte == ZSH_META:
            nxt = next(it, None)
            if nxt is None:  # dangling Meta at the very end: drop it
                break
            out.append(nxt ^ 0x20)
        else:
            out.append(byte)
    return bytes(out)


def normalize_command(text: str) -> str | None:
    """Return a cleaned-up command, or ``None`` if there is nothing to keep.

    Only whitespace at the edges is removed, and only where that cannot change
    meaning: ``echo a\\ `` (escaped trailing space) keeps its space.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.endswith("\\") and len(text.rstrip()) < len(text):
        # Keep the single whitespace character the backslash escapes.
        start = len(text) - len(text.lstrip())
        stripped = text[start : start + len(stripped) + 1]
    return stripped


def _clean_command(text: str, stats: ParseStats) -> str | None:
    command = normalize_command(text)
    if command is None:
        stats.blank += 1
        return None
    if _UNSAFE_CHARS_RE.search(command):
        stats.unsafe += 1
        return None
    if len(command) > MAX_COMMAND_LENGTH:
        stats.too_long += 1
        return None
    return command


def _decode_history_bytes(raw: bytes, stats: ParseStats) -> str | None:
    try:
        text = unmetafy(raw).decode("utf-8")
    except UnicodeDecodeError:
        # Not metafied (e.g. written by another tool)? Try the raw bytes.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            stats.undecodable += 1
            return None
    return _clean_command(text, stats)


def check_readable(path: Path, label: str) -> None:
    """Raise ``HistoryError`` with an actionable message unless ``path`` is readable."""
    try:
        st = path.stat()
    except FileNotFoundError:
        raise HistoryError(f"{label} not found: {path}") from None
    except PermissionError:
        raise HistoryError(f"permission denied reading {label}: {path}") from None
    except OSError as exc:
        raise HistoryError(f"cannot access {label} {path}: {exc.strerror}") from None
    if not stat.S_ISREG(st.st_mode):
        raise HistoryError(f"{label} is not a regular file: {path}")
    if not os.access(path, os.R_OK):
        raise HistoryError(f"permission denied reading {label}: {path}")


def _iter_logical_lines(path: Path, start: int, label: str) -> Iterator[tuple[bytes, int, bool]]:
    """Yield ``(logical_line, end_offset, complete)`` honouring zsh continuations.

    ``complete`` is False only for a trailing entry with no final newline,
    which may still be in the middle of being written by zsh.
    """
    try:
        handle = path.open("rb")
    except PermissionError:
        raise HistoryError(f"permission denied reading {label}: {path}") from None
    except OSError as exc:
        raise HistoryError(f"cannot open {label} {path}: {exc.strerror}") from None
    with handle:
        handle.seek(start)
        offset = start
        pending = bytearray()
        for line in handle:
            offset += len(line)
            if not line.endswith(b"\n"):
                pending += line
                yield bytes(pending), offset, False
                return
            body = line[:-1]
            if body.endswith(b"\\"):
                pending += body[:-1] + b"\n"
                continue
            pending += body
            yield bytes(pending), offset, True
            pending.clear()
        if pending:  # file ended right after a continuation backslash
            yield bytes(pending), offset, False


def iter_history(
    path: Path,
    *,
    start_offset: int = 0,
    include_incomplete: bool = True,
    stats: ParseStats | None = None,
) -> Iterator[HistoryEntry]:
    """Stream entries from a zsh history file.

    ``start_offset`` must be an ``end_offset`` previously returned by this
    function (or 0). Set ``include_incomplete=False`` when indexing so a line
    that zsh is still writing is picked up on the next refresh instead.
    """
    stats = stats if stats is not None else ParseStats()
    stats.end_offset = start_offset
    check_readable(path, "history file")
    for raw, end, complete in _iter_logical_lines(path, start_offset, "history file"):
        if not complete and not include_incomplete:
            return
        if complete:
            stats.end_offset = end
        timestamp: int | None = None
        match = _EXTENDED_RE.match(raw)
        if match:
            timestamp = int(match.group(1))
            raw = match.group(3)
        command = _decode_history_bytes(raw, stats)
        if command is None:
            continue
        stats.entries += 1
        yield HistoryEntry(command=command, timestamp=timestamp, end_offset=end)


def read_history(path: Path) -> tuple[list[HistoryEntry], ParseStats]:
    """Convenience wrapper: parse a whole history file into memory."""
    stats = ParseStats()
    entries = list(iter_history(path, stats=stats))
    return entries, stats


# --- Directory-context log -------------------------------------------------
#
# Written by the zsh integration (pure zsh, no Python) from ``preexec``:
#     <epoch>\t<escaped directory>\t<escaped command>\n
# where backslash, newline and tab are escaped as \\, \n and \t.

_ESCAPES = {"\\": "\\", "n": "\n", "t": "\t"}
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def unescape_field(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        try:
            return _ESCAPES[m.group(1)]
        except KeyError:
            raise ValueError(f"bad escape \\{m.group(1)}") from None

    return _ESCAPE_RE.sub(repl, text)


def escape_field(text: str) -> str:
    """Inverse of :func:`unescape_field` (mirrors the zsh writer; used in tests)."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")


def iter_context_log(
    path: Path, *, start_offset: int = 0, stats: ParseStats | None = None
) -> Iterator[ContextEntry]:
    stats = stats if stats is not None else ParseStats()
    stats.end_offset = start_offset
    check_readable(path, "context log")
    for raw, end, complete in _iter_raw_lines(path, start_offset):
        if not complete:
            return
        stats.end_offset = end
        parts = raw.split(b"\t")
        if len(parts) != 3 or not parts[0].isdigit():
            stats.malformed += 1
            continue
        try:
            directory = unescape_field(parts[1].decode("utf-8"))
            command_text = unescape_field(parts[2].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            stats.malformed += 1
            continue
        command = _clean_command(command_text, stats)
        if command is None:
            continue
        if not os.path.isabs(directory):
            stats.malformed += 1
            continue
        stats.entries += 1
        yield ContextEntry(command, directory, int(parts[0]), end)


def _iter_raw_lines(path: Path, start: int) -> Iterator[tuple[bytes, int, bool]]:
    # The context log escapes newlines itself, so no continuation handling.
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise HistoryError(f"cannot open context log {path}: {exc.strerror}") from None
    with handle:
        handle.seek(start)
        offset = start
        for line in handle:
            offset += len(line)
            complete = line.endswith(b"\n")
            yield (line[:-1] if complete else line), offset, complete
