from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from termpilot.cli import main
from termpilot.integration import (
    BEGIN_MARKER,
    END_MARKER,
    IntegrationError,
    install,
    is_installed,
    remove_block,
    snippet,
    uninstall,
    zsh_script_path,
)

ORIGINAL = "export EDITOR=vim\nalias ll='ls -l'\n"


def test_script_ships_with_package_and_parses() -> None:
    path = zsh_script_path()
    assert path.is_file()
    result = subprocess.run(["zsh", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_has_no_eval() -> None:
    code = "\n".join(
        line
        for line in zsh_script_path().read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "eval" not in code


def test_snippet_quotes_paths() -> None:
    text = snippet()
    assert text.startswith(BEGIN_MARKER) and text.rstrip().endswith(END_MARKER)
    assert f"source '{zsh_script_path()}'" in text or f"source {zsh_script_path()}" in text


def test_install_appends_block_with_backup(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    result = install(rc)
    assert result.changed and result.backup is not None
    assert result.backup.read_text() == ORIGINAL
    text = rc.read_text()
    assert text.startswith(ORIGINAL) and BEGIN_MARKER in text
    assert is_installed(rc)


def test_install_is_idempotent(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    install(rc)
    once = rc.read_text()
    assert not install(rc).changed
    assert rc.read_text() == once


def test_install_creates_missing_file(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    result = install(rc)
    assert result.changed and result.backup is None and is_installed(rc)


def test_uninstall_restores_original(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    install(rc)
    with rc.open("a") as f:
        f.write("# added later by the user\n")
    result = uninstall(rc)
    assert result.changed and result.backup is not None
    assert rc.read_text() == ORIGINAL + "# added later by the user\n"
    assert not uninstall(rc).changed


def test_symlinked_zshrc_stays_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "zshrc"
    real.parent.mkdir()
    real.write_text(ORIGINAL)
    rc = tmp_path / ".zshrc"
    rc.symlink_to(real)
    install(rc)
    assert rc.is_symlink() and BEGIN_MARKER in real.read_text()
    uninstall(rc)
    assert rc.is_symlink() and real.read_text() == ORIGINAL


def test_uninstall_preserves_permissions(tmp_path: Path) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    rc.chmod(0o600)
    install(rc)
    uninstall(rc)
    assert oct(rc.stat().st_mode & 0o777) == "0o600"


def test_unterminated_block_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IntegrationError, match="by hand"):
        remove_block(f"a\n{BEGIN_MARKER}\nsource x\n")


def test_cli_install_requires_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["install-zsh", "--zshrc", str(rc)]) == 4
    assert rc.read_text() == ORIGINAL
    assert "--yes" in capsys.readouterr().err


def test_cli_install_interactive_decline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert main(["install-zsh", "--zshrc", str(rc)]) == 0
    assert rc.read_text() == ORIGINAL
    assert "nothing changed" in capsys.readouterr().out


def test_cli_install_and_uninstall_with_yes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = tmp_path / ".zshrc"
    rc.write_text(ORIGINAL)
    assert main(["install-zsh", "--zshrc", str(rc), "--yes"]) == 0
    assert is_installed(rc)
    assert main(["install-zsh", "--zshrc", str(rc), "--yes"]) == 0
    assert "already installed" in capsys.readouterr().out
    assert main(["uninstall-zsh", "--zshrc", str(rc), "--yes"]) == 0
    assert rc.read_text() == ORIGINAL


def test_cli_init_and_paths(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["init", "zsh"]) == 0
    out = capsys.readouterr().out
    assert BEGIN_MARKER in out and "source" in out
    assert main(["paths"]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert lines["context_log"].endswith("context.log")
    assert lines["database"].endswith("index.db")


def test_paths_reports_disabled_context(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("context = false\n")
    assert main(["--config", str(cfg), "paths"]) == 0
    assert "context_log=\n" in capsys.readouterr().out
