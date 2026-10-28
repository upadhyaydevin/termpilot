from __future__ import annotations

from pathlib import Path

import pytest

from termpilot.config import ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "absent.toml")
    assert cfg.config_path is None
    assert cfg.ranking.max_suggestions == 5
    assert cfg.context_enabled is True
    assert cfg.db_path.name == "index.db"


def test_file_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERMPILOT_DATA_DIR")
    path = write(
        tmp_path,
        'history_path = "~/hist"\ndata_dir = "/tmp/tp"\ncontext = false\n'
        "[ranking]\nmin_input_length = 3\nweight_recency = 0.5\n",
    )
    cfg = load_config(path)
    assert cfg.history_path == Path("~/hist").expanduser()
    assert cfg.data_dir == Path("/tmp/tp")
    assert cfg.context_enabled is False
    assert cfg.ranking.min_input_length == 3
    assert cfg.ranking.weight_recency == 0.5


def test_precedence_cli_over_env_over_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, 'history_path = "/from/file"\n')
    monkeypatch.setenv("HISTFILE", "/from/histfile")
    assert load_config(path).history_path == Path("/from/file")
    monkeypatch.setenv("TERMPILOT_HISTORY", "/from/env")
    assert load_config(path).history_path == Path("/from/env")
    assert load_config(path, history_path=Path("/from/cli")).history_path == Path("/from/cli")


def test_histfile_is_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HISTFILE", "/custom/hist")
    assert load_config(tmp_path / "absent.toml").history_path == Path("/custom/hist")


@pytest.mark.parametrize(
    "text",
    [
        "bogus = 1\n",
        "[ranking]\nweight_typo = 1\n",
        "[ranking]\nmin_input_length = 2.5\n",
        "[ranking]\nweight_match = true\n",
        "[ranking]\nweight_match = -1\n",
        "context = 'yes'\n",
        "this is not toml",
    ],
)
def test_invalid_config_raises(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))
