"""Opt-in Zsh setup: print the snippet, or add/remove it from ~/.zshrc.

The installer only ever touches a clearly marked block, makes a timestamped
backup first, and follows symlinks (dotfile managers often symlink .zshrc).
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from termpilot.errors import TermPilotError

BEGIN_MARKER = "# >>> termpilot >>>"
END_MARKER = "# <<< termpilot <<<"


class IntegrationError(TermPilotError):
    """The shell configuration could not be read or updated."""


def zsh_script_path() -> Path:
    return Path(__file__).resolve().parent / "shell" / "termpilot.zsh"


def default_zshrc() -> Path:
    zdotdir = os.environ.get("ZDOTDIR")
    base = Path(zdotdir) if zdotdir else Path(os.environ.get("HOME") or Path.home())
    return base / ".zshrc"


def termpilot_executable() -> str:
    """Absolute path of the running ``termpilot`` (works from an unactivated venv)."""
    found = shutil.which("termpilot")
    if found:
        return str(Path(found).resolve())
    argv0 = Path(sys.argv[0])
    if argv0.name == "termpilot" and argv0.exists():
        return str(argv0.resolve())
    return "termpilot"


def snippet() -> str:
    lines = [
        BEGIN_MARKER,
        "# Added by `termpilot install-zsh`; remove with `termpilot uninstall-zsh`.",
    ]
    exe = termpilot_executable()
    if exe != "termpilot":
        lines.append(f"TERMPILOT_BIN={shlex.quote(exe)}")
    lines.append(f"source {shlex.quote(str(zsh_script_path()))}")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise IntegrationError(f"cannot read {path}: {exc}") from None


def is_installed(zshrc: Path) -> bool:
    return BEGIN_MARKER in _read(zshrc.resolve())


def _backup(target: Path) -> Path | None:
    if not target.exists():
        return None
    backup = target.with_name(f"{target.name}.termpilot-backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)
    return backup


@dataclass(frozen=True, slots=True)
class ChangeResult:
    changed: bool
    target: Path
    backup: Path | None = None


def install(zshrc: Path) -> ChangeResult:
    target = zshrc.resolve()
    current = _read(target)
    if BEGIN_MARKER in current:
        return ChangeResult(False, target)
    try:
        backup = _backup(target)
        with target.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write("\n" + snippet())
    except OSError as exc:
        raise IntegrationError(f"cannot update {target}: {exc.strerror}") from None
    return ChangeResult(True, target, backup)


def remove_block(text: str) -> str:
    """Remove every marked TermPilot block (and the blank line before it)."""
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == BEGIN_MARKER:
            skipping = True
            if out and not out[-1].strip():
                out.pop()
            continue
        if skipping:
            if stripped == END_MARKER:
                skipping = False
            continue
        out.append(line)
    if skipping:
        raise IntegrationError(
            f"found '{BEGIN_MARKER}' without '{END_MARKER}'; please edit the file by hand"
        )
    return "".join(out)


def uninstall(zshrc: Path) -> ChangeResult:
    target = zshrc.resolve()
    current = _read(target)
    if BEGIN_MARKER not in current:
        return ChangeResult(False, target)
    updated = remove_block(current)
    try:
        backup = _backup(target)
        # Write a sibling temp file then rename, so a crash never leaves a
        # half-written .zshrc. Permissions are copied from the original.
        tmp = target.with_name(f".{target.name}.termpilot-tmp")
        tmp.write_text(updated, encoding="utf-8")
        shutil.copymode(target, tmp)
        tmp.replace(target)
    except OSError as exc:
        raise IntegrationError(f"cannot update {target}: {exc.strerror}") from None
    return ChangeResult(True, target, backup)
