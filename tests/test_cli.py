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


def test_doctor_healthy(capsys: pytest.CaptureFixture[str], sample_history: Path) -> None:
    code, out, _ = run(capsys, "--history", str(sample_history), "doctor")
    assert code == 0, out
    assert "all checks passed" in out
    assert "10 commands" in out
    assert "git status" not in out  # never prints history content


def test_doctor_reports_failures(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _ = run(
        capsys, "--history", str(tmp_path / "nope"), "--data-dir", str(tmp_path / "x"), "doctor"
    )
    assert code == 4
    assert "FAIL  history" in out and "FAIL  index" in out
    assert "termpilot index" in out


def test_doctor_detects_corrupt_index(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    data = tmp_path / "corrupt"
    data.mkdir()
    (data / "index.db").write_bytes(b"garbage" * 200)
    code, out, _ = run(capsys, "--data-dir", str(data), "doctor")
    assert code == 4 and "FAIL  index" in out and "--rebuild" in out


def test_suggest_on_corrupt_index_is_actionable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    data = tmp_path / "corrupt"
    data.mkdir()
    (data / "index.db").write_bytes(b"garbage" * 200)
    code, out, err = run(capsys, "--data-dir", str(data), "suggest", "git")
    assert code == 4 and out == "" and "--rebuild" in err


def test_index_recovers_corrupt_db(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sample_history: Path
) -> None:
    data = tmp_path / "corrupt"
    data.mkdir()
    (data / "index.db").write_bytes(b"garbage" * 200)
    code, _, err = run(capsys, "--data-dir", str(data), "--history", str(sample_history), "index")
    assert code == 0 and "moved it to" in err
    code, out, _ = run(capsys, "--data-dir", str(data), "suggest", "docker")
    assert code == 0 and "docker compose up -d" in out


def test_purge_requires_confirmation_noninteractive(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, _, err = run(capsys, "purge")
    assert code == 4 and "--yes" in err
    assert (tmp_path / "data" / "index.db").exists()


def test_purge_deletes_only_termpilot_files(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, sample_history: Path
) -> None:
    data = tmp_path / "data"
    (data / "context.log").write_text("1\t/x\tls\n")
    (data / "unrelated.txt").write_text("keep me")
    before = sample_history.read_bytes()
    code, out, _ = run(capsys, "purge", "--yes")
    assert code == 0 and "deleted" in out
    assert not (data / "index.db").exists() and not (data / "context.log").exists()
    assert (data / "unrelated.txt").exists()
    assert sample_history.read_bytes() == before
    code, out, _ = run(capsys, "purge", "--yes")
    assert code == 0 and "nothing to delete" in out


@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_invalid_max_is_usage_error(capsys: pytest.CaptureFixture[str], value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["suggest", "-n", value, "git"])
    assert exc.value.code == 2


def test_unexpected_errors_are_concise_unless_debug(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("termpilot.cli.rank", boom)
    code, _, err = run(capsys, "suggest", "git")
    assert code == 70 and "kaboom" in err and "Traceback" not in err
    with pytest.raises(RuntimeError):
        main(["--debug", "suggest", "git"])


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert capsys.readouterr().out.startswith("termpilot 0.1.0")
