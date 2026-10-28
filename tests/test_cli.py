from __future__ import annotations

import json
from pathlib import Path

import pytest

from termpilot.cli import main


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


@pytest.fixture(autouse=True)
def indexed(capsys: pytest.CaptureFixture[str], sample_history: Path) -> None:
    """Every test starts with the synthetic fixture indexed into a temp data dir."""
    assert run(capsys, "--history", str(sample_history), "index", "--quiet")[0] == 0


def test_suggest_text(capsys: pytest.CaptureFixture[str], sample_history: Path) -> None:
    code, out, _ = run(capsys, "--history", str(sample_history), "suggest", "git che")
    assert code == 0
    assert out.splitlines()[0].startswith("git checkout")


def test_suggest_inline_skips_exact(
    capsys: pytest.CaptureFixture[str], sample_history: Path
) -> None:
    code, out, _ = run(
        capsys, "--history", str(sample_history), "suggest", "--format", "inline", "git status"
    )
    assert code == 1 and out == ""
    code, out, _ = run(
        capsys, "--history", str(sample_history), "suggest", "--format", "inline", "docker"
    )
    assert code == 0 and out == "docker compose up -d"


def test_suggest_json_and_explain(
    capsys: pytest.CaptureFixture[str], sample_history: Path
) -> None:
    code, out, _ = run(
        capsys, "--history", str(sample_history), "suggest", "--format", "json", "-n", "2", "git"
    )
    data = json.loads(out)
    assert code == 0 and len(data) == 2
    assert {"command", "score", "match", "features"} <= set(data[0])
    code, out, _ = run(capsys, "--history", str(sample_history), "suggest", "--explain", "git")
    assert "match=" in out and "recency=" in out


def test_suggest_no_match(capsys: pytest.CaptureFixture[str], sample_history: Path) -> None:
    code, out, _ = run(capsys, "--history", str(sample_history), "suggest", "qqqqqq")
    assert code == 1 and out == ""


def test_missing_history_is_actionable(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run(capsys, "--history", str(tmp_path / "nope"), "index")
    assert code == 4
    assert "not found" in err


def test_suggest_without_index(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, _, err = run(capsys, "--data-dir", str(tmp_path / "empty"), "suggest", "git")
    assert code == 4
    assert "termpilot index" in err


def test_index_reports_progress(capsys: pytest.CaptureFixture[str], sample_history: Path) -> None:
    code, out, _ = run(capsys, "--history", str(sample_history), "index")
    assert code == 0
    assert "history:  unchanged" in out
    code, out, _ = run(capsys, "--history", str(sample_history), "index", "--rebuild")
    assert "history:  full, 12 new entries" in out


def test_stats(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "stats")
    assert code == 0
    assert "commands:     10" in out
    assert "git" not in out


def test_bad_config(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text("[ranking]\nweight_match = 'heavy'\n")
    code, _, err = run(capsys, "--config", str(cfg), "suggest", "git")
    assert code == 3 and "weight_match" in err


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "suggest" in capsys.readouterr().out
