"""End-to-end tests that drive a real interactive zsh through a pseudo-terminal.

They type into a zsh that has sourced termpilot.zsh (with an isolated HOME,
data dir and synthetic history) and inspect what the terminal shows. Skipped
when zsh is not installed.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import termios
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from termpilot.cli import main
from termpilot.integration import zsh_script_path

pytestmark = [
    pytest.mark.zsh,
    pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed"),
]

PROMPT = "TPREADY> "
RIGHT_ARROW = "\x1b[C"
UP_ARROW = "\x1b[A"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>]|[\r\x07]")

HISTORY = [
    "echo $((40+2))-accepted",
    "echo $((5+5))x; echo GHOST$((6*7))",
    "echo first-cmd-and-more",
    "ls -la",
]


class ZshSession:
    def __init__(self, env: dict[str, str], cwd: Path) -> None:
        pid, fd = pty.fork()
        if pid == 0:  # child
            os.chdir(cwd)
            os.execvpe("zsh", ["zsh", "-d", "-i"], env)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
        self.raw = ""

    @property
    def screen(self) -> str:
        """Everything printed so far, with terminal control sequences removed."""
        return _ANSI.sub("", self.raw)

    def _read(self, timeout: float) -> None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if ready:
            try:
                self.raw += os.read(self.fd, 65536).decode("utf-8", "replace")
            except OSError:
                pass

    def expect(self, text: str, timeout: float = 8.0, start: int = 0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pos = self.screen.find(text, start)
            if pos >= 0:
                return pos
            self._read(0.05)
        raise AssertionError(f"timed out waiting for {text!r}; screen tail:\n{self.screen[-800:]}")

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._read(0.05)

    def send(self, keys: str) -> None:
        os.write(self.fd, keys.encode())

    def mark(self) -> int:
        return len(self.screen)

    def close(self) -> None:
        try:
            self.send("\x15exit\r")
            self.settle(0.3)
        finally:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
            os.close(self.fd)


def write_backend(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def zsh_env(tmp_path: Path) -> dict[str, str]:
    history = tmp_path / "zsh_history"
    history.write_text("".join(f": {1_700_000_000 + i}:0;{c}\n" for i, c in enumerate(HISTORY)))
    assert main(["--history", str(history), "index", "--quiet"]) == 0

    backend = write_backend(
        tmp_path / "termpilot-bin", f'exec "{sys.executable}" -m termpilot "$@"'
    )
    zdotdir = tmp_path / "zdot"
    zdotdir.mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "ZDOTDIR": str(zdotdir),
        "PATH": "/usr/bin:/bin",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "TERMPILOT_DATA_DIR": os.environ["TERMPILOT_DATA_DIR"],
        "TERMPILOT_CONFIG": os.environ["TERMPILOT_CONFIG"],
        "TP_HISTFILE": str(history),
        "TP_BACKEND": str(backend),
        "TP_SCRIPT": str(zsh_script_path()),
    }
    (zdotdir / ".zshrc").write_text(
        "HISTFILE=$TP_HISTFILE\nHISTSIZE=1000\nSAVEHIST=1000\n"
        f"PS1='{PROMPT}'\nPROMPT_EOL_MARK=''\nbindkey -e\n"
        'TERMPILOT_BIN=$TP_BACKEND\nsource "$TP_SCRIPT"\n'
    )
    return env


@pytest.fixture
def session(zsh_env: dict[str, str], tmp_path: Path) -> Iterator[ZshSession]:
    s = ZshSession(zsh_env, tmp_path)
    s.expect(PROMPT)
    yield s
    s.close()


def test_ghost_text_appears_and_right_arrow_accepts(session: ZshSession) -> None:
    session.send("echo $((4")
    session.expect("0+2))-accepted")  # the dimmed remainder
    session.send(RIGHT_ARROW)
    m = session.mark()
    session.send("\r")
    session.expect("42-accepted", start=m)


def test_suggestion_is_never_executed_automatically(session: ZshSession) -> None:
    session.send("echo $((5+5))x")
    session.expect("; echo GHOST$((6*7))")
    session.settle(1.5)
    assert "GHOST42" not in session.screen
    # Enter runs only what was typed, never the ghost text.
    m = session.mark()
    session.send("\r")
    session.expect("10x", start=m)
    session.expect(PROMPT, start=m)
    session.settle(0.5)
    assert "GHOST42" not in session.screen


def test_end_of_line_accepts_and_plain_editing_still_works(session: ZshSession) -> None:
    session.send("ls -")
    session.expect("la")
    session.send("\x05")  # Ctrl-E (end-of-line) accepts
    session.send("\x01")  # Ctrl-A: normal editing still works
    session.send("echo ")  # line is now "echo ls -la"
    m = session.mark()
    session.send("\r")
    session.expect("ls -la", start=m)


def test_history_navigation_is_not_decorated(session: ZshSession) -> None:
    session.send("echo first-cmd\r")
    session.expect(PROMPT, start=session.expect("first-cmd"))
    m = session.mark()
    session.send(UP_ARROW)
    session.expect("echo first-cmd", start=m)
    session.settle(1.5)
    assert "-and-more" not in session.screen[m:]


def test_disable_and_enable(session: ZshSession) -> None:
    session.send("termpilot_disable\r")
    session.expect(PROMPT, start=session.expect("termpilot_disable"))
    m = session.mark()
    session.send("echo $((4")
    session.settle(1.5)
    assert "-accepted" not in session.screen[m:]
    session.send(RIGHT_ARROW + "\x15termpilot_enable\r")  # Ctrl-U clears the line
    session.expect(PROMPT, start=session.expect("termpilot_enable", start=m))
    session.send("echo $((4")
    session.expect("0+2))-accepted", start=m)


@pytest.mark.parametrize(
    "backend",
    [
        'echo "Traceback: something broke" >&2; echo "\033[31mgarbage"; exit 3',
        '[ "$1" = suggest ] && exec sleep 30; exit 0',
    ],
    ids=["crashing-backend", "hanging-backend"],
)
def test_broken_backend_never_blocks_the_shell(
    zsh_env: dict[str, str], tmp_path: Path, backend: str
) -> None:
    zsh_env["TP_BACKEND"] = str(write_backend(tmp_path / "broken-bin", backend))
    s = ZshSession(zsh_env, tmp_path)
    try:
        s.expect(PROMPT)
        start = time.monotonic()
        s.send("echo $((6*7))-ok\r")
        s.expect("42-ok", timeout=5)
        assert time.monotonic() - start < 3
        assert "Traceback" not in s.screen
    finally:
        s.close()


def test_context_log_and_background_index(session: ZshSession, tmp_path: Path) -> None:
    proj = tmp_path / "proj dir"  # a space in the path on purpose
    proj.mkdir()
    session.send(f"cd '{proj}' && echo ctx-marker-$((1+1))\r")
    session.expect("ctx-marker-2")
    log = Path(os.environ["TERMPILOT_DATA_DIR"]) / "context.log"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not (log.exists() and "ctx-marker" in log.read_text()):
        time.sleep(0.1)
    line = log.read_text().strip().splitlines()[-1]
    assert line.split("\t")[1] == str(tmp_path)  # preexec runs before the cd
    assert oct(log.stat().st_mode & 0o777) == "0o600"

    # The precmd hook refreshes the index in the background.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if main(["suggest", "--format", "inline", "echo ctx-mar"]) == 0:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("background index never picked up the new command")
