from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_history() -> Path:
    return FIXTURES / "sample_zsh_history"


@pytest.fixture
def write_history(tmp_path: Path) -> Callable[..., Path]:
    """Write raw bytes to a temporary history file and return its path."""

    def _write(data: bytes, name: str = "zsh_history") -> Path:
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _write


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test away from the real config, data dir and shell history."""
    monkeypatch.setenv("TERMPILOT_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("TERMPILOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HISTFILE", str(tmp_path / "missing_history"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
