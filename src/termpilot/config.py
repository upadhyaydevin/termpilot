"""Configuration: defaults, an optional TOML file, environment, CLI flags.

Precedence, highest first:
  1. CLI flags (``--history``, ``--data-dir``, ``-n``)
  2. Environment: ``TERMPILOT_CONFIG``, ``TERMPILOT_DATA_DIR``,
     ``TERMPILOT_HISTORY``
  3. The config file (``~/.config/termpilot/config.toml`` by default)
  4. Built-in defaults (history path falls back to ``$HISTFILE``, then
     ``~/.zsh_history``)
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from termpilot.ranking import RankingConfig

APP_NAME = "termpilot"


class ConfigError(Exception):
    """The configuration file is missing required structure or has bad values."""


@dataclass(frozen=True, slots=True)
class Config:
    history_path: Path
    data_dir: Path
    # Record (directory, command) pairs from the zsh hook to boost suggestions
    # for commands previously run in the current directory.
    context_enabled: bool = True
    ranking: RankingConfig = field(default_factory=RankingConfig)
    config_path: Path | None = None  # the file actually loaded, if any

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def context_log_path(self) -> Path:
        return self.data_dir / "context.log"


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def default_config_path() -> Path:
    env = os.environ.get("TERMPILOT_CONFIG")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else _home() / ".config"
    return base / APP_NAME / "config.toml"


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else _home() / ".local" / "share") / APP_NAME


def default_history_path() -> Path:
    histfile = os.environ.get("HISTFILE")
    return Path(histfile).expanduser() if histfile else _home() / ".zsh_history"


def _expect(value: Any, kind: type | tuple[type, ...], key: str) -> Any:
    # bool is a subclass of int; don't let `weight_match = true` through.
    if isinstance(value, bool) and kind is not bool:
        raise ConfigError(f"{key}: expected a number, got a boolean")
    if not isinstance(value, kind):
        raise ConfigError(f"{key}: expected {getattr(kind, '__name__', 'number')}, got {value!r}")
    return value


def _ranking_from_table(table: dict[str, Any]) -> RankingConfig:
    defaults = RankingConfig()
    known = {f.name for f in fields(RankingConfig)}
    values: dict[str, Any] = {}
    for key, value in table.items():
        if key not in known:
            raise ConfigError(f"unknown key [ranking].{key}")
        if isinstance(getattr(defaults, key), int):
            values[key] = _expect(value, int, f"ranking.{key}")
        else:
            values[key] = float(_expect(value, (int, float), f"ranking.{key}"))
        if values[key] < 0:
            raise ConfigError(f"ranking.{key} must not be negative")
    return replace(RankingConfig(), **values)


def load_config(
    path: Path | None = None,
    *,
    history_path: Path | None = None,
    data_dir: Path | None = None,
    max_suggestions: int | None = None,
) -> Config:
    """Build the effective configuration; keyword arguments are CLI overrides."""
    config_path = path or default_config_path()
    data: dict[str, Any] = {}
    loaded: Path | None = None
    if config_path.is_file():
        try:
            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {config_path}: {exc}") from None
        except OSError as exc:
            raise ConfigError(f"cannot read {config_path}: {exc.strerror}") from None
        loaded = config_path

    allowed = {"history_path", "data_dir", "context", "ranking"}
    for key in data:
        if key not in allowed:
            raise ConfigError(f"unknown key '{key}' in {config_path}")

    ranking = _ranking_from_table(_expect(data.get("ranking", {}), dict, "ranking"))
    if max_suggestions is not None:
        ranking = replace(ranking, max_suggestions=max_suggestions)

    def pick_path(cli: Path | None, env_var: str, key: str, default: Path) -> Path:
        if cli is not None:
            return cli.expanduser()
        if os.environ.get(env_var):
            return Path(os.environ[env_var]).expanduser()
        if key in data:
            return Path(_expect(data[key], str, key)).expanduser()
        return default

    return Config(
        history_path=pick_path(
            history_path, "TERMPILOT_HISTORY", "history_path", default_history_path()
        ),
        data_dir=pick_path(data_dir, "TERMPILOT_DATA_DIR", "data_dir", default_data_dir()),
        context_enabled=_expect(data.get("context", True), bool, "context"),
        ranking=ranking,
        config_path=loaded,
    )
