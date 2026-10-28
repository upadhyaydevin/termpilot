from __future__ import annotations

from types import SimpleNamespace

from termpilot.ranking import (
    Candidate,
    RankingConfig,
    Suggestion,
    fuzzy_score,
    match_quality,
    merge_duplicates,
    pick_inline,
    rank,
)

NOW = 1_700_000_000.0
DAY = 86400.0


def commands(results: list[Suggestion]) -> list[str]:
    return [s.command for s in results]


def test_prefix_match_beats_fuzzy() -> None:
    cands = [
        Candidate("git checkout main", count=3, last_used=NOW - DAY),
        Candidate("go test ./internal/checker", count=3, last_used=NOW - DAY),
    ]
    assert commands(rank("git che", cands, now=NOW)) == ["git checkout main"]
    # The fuzzy candidate does still match a fuzzy query.
    assert "go test ./internal/checker" in commands(rank("gotest", cands, now=NOW))


def test_match_kinds() -> None:
    assert match_quality("git", "git status")[1] == "prefix"
    assert match_quality("GIT", "git status")[1] == "prefix-caseless"
    assert match_quality("g ch ma", "git checkout main")[1] == "token-prefix"
    assert match_quality("gcm", "git checkout main")[1] == "fuzzy"
    assert match_quality("xyz", "git checkout main") == (0.0, "none")


def test_fuzzy_prefers_word_starts_and_runs() -> None:
    assert fuzzy_score("gco", "git checkout") > fuzzy_score("gco", "xgxcxo")
    assert fuzzy_score("abc", "ab") == 0.0
    assert fuzzy_score("", "ls") == 0.0


def test_recency_breaks_otherwise_equal_matches() -> None:
    cands = [
        Candidate("git push origin old", count=5, last_used=NOW - 30 * DAY),
        Candidate("git push origin new", count=5, last_used=NOW - 1 * DAY),
    ]
    assert commands(rank("git push", cands, now=NOW))[0] == "git push origin new"


def test_frequency_matters() -> None:
    cands = [
        Candidate("make build", count=1, last_used=NOW - DAY),
        Candidate("make test", count=80, last_used=NOW - DAY),
    ]
    assert commands(rank("make", cands, now=NOW))[0] == "make test"


def test_context_boost() -> None:
    cands = [
        Candidate("npm run dev", count=5, last_used=NOW - DAY),
        Candidate("npm run lint", count=5, last_used=NOW - DAY, dir_count=10),
    ]
    assert commands(rank("npm run", cands, now=NOW))[0] == "npm run lint"


def test_frequent_but_unrelated_commands_are_not_suggested() -> None:
    cands = [Candidate("ls -la", count=10_000, last_used=NOW), Candidate("terraform plan")]
    assert commands(rank("terr", cands, now=NOW)) == ["terraform plan"]
    assert rank("zzzz", cands, now=NOW) == []


def test_empty_and_short_input_return_nothing() -> None:
    cands = [Candidate("ls"), Candidate("git status")]
    assert rank("", cands, now=NOW) == []
    assert rank("   ", cands, now=NOW) == []
    assert rank("g", cands, now=NOW) == []
    assert rank("g", cands, now=NOW, config=RankingConfig(min_input_length=1)) != []


def test_trailing_space_is_significant_leading_space_is_not() -> None:
    cands = [Candidate("gitk"), Candidate("git log")]
    assert commands(rank("git ", cands, now=NOW))[0] == "git log"
    assert commands(rank("  gitk", cands, now=NOW))[0] == "gitk"


def test_duplicates_are_merged() -> None:
    cands = [
        Candidate("ls -la", count=2, last_used=NOW - 10),
        Candidate("ls -la", count=3, last_used=NOW - 5, dir_count=1),
    ]
    merged = merge_duplicates(cands)
    assert merged == [Candidate("ls -la", count=5, last_used=NOW - 5, dir_count=1)]
    assert commands(rank("ls", cands, now=NOW)) == ["ls -la"]


def test_ties_are_deterministic() -> None:
    cands = [Candidate(f"echo {c}", count=1, last_used=NOW) for c in "dbca"]
    first = commands(rank("echo", cands, now=NOW))
    assert first == ["echo a", "echo b", "echo c", "echo d"]
    assert commands(rank("echo", list(reversed(cands)), now=NOW)) == first


def test_malformed_candidates_do_not_crash() -> None:
    cands = [
        None,
        42,
        SimpleNamespace(command=None),
        SimpleNamespace(command="   "),
        SimpleNamespace(command="git log", count="lots", last_used="yesterday", dir_count=-3),
        SimpleNamespace(command="git diff", count=float("nan"), last_used=float("inf")),
        Candidate("git status", count=-5, last_used=None),
    ]
    results = rank("git", cands, now=NOW)
    assert sorted(commands(results)) == ["git diff", "git log", "git status"]
    assert all(0 <= s.score <= 1 for s in results)


def test_limit_and_explanations() -> None:
    cands = [Candidate(f"cmd {i}", count=i + 1, last_used=NOW - i * DAY) for i in range(20)]
    results = rank("cmd", cands, now=NOW, limit=3)
    assert len(results) == 3
    assert set(results[0].features) == {"match", "recency", "frequency", "context"}
    assert results[0].score >= results[1].score >= results[2].score
    assert rank("cmd", cands, now=NOW, limit=0) == []


def test_no_timestamps_still_ranks() -> None:
    cands = [Candidate("docker ps"), Candidate("docker ps -a", count=4)]
    assert commands(rank("dock", cands, now=NOW)) == ["docker ps -a", "docker ps"]


def test_pick_inline_skips_exact_match() -> None:
    cands = [
        Candidate("git status", count=50, last_used=NOW),
        Candidate("git status -sb", count=1, last_used=NOW - DAY),
    ]
    results = rank("git status", cands, now=NOW)
    assert results[0].command == "git status"
    chosen = pick_inline("git status", results)
    assert chosen is not None and chosen.command == "git status -sb"
    assert pick_inline("ls", []) is None


def _is_subsequence(query: str, text: str) -> bool:
    it = iter(text.lower())
    return all(ch in it for ch in query.lower())


def test_prefilter_matches_reference_subsequence_check() -> None:
    import random

    from termpilot.ranking import _fuzzy_prefilter

    rnd = random.Random(1)
    alphabet = "ab-. *\\x"
    for _ in range(3000):
        q = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 5)))
        text = "".join(rnd.choice(alphabet + "AB") for _ in range(rnd.randint(0, 12)))
        assert bool(_fuzzy_prefilter(q).match(text)) == _is_subsequence(q, text), (q, text)


def test_pathological_input_does_not_backtrack() -> None:
    # Regression: the original `a.*?b.*?c` prefilter backtracked
    # catastrophically here (minutes per candidate).
    import time

    cands = [Candidate("echo " + "-" * 400), Candidate("a" * 4000), Candidate("-" * 4000)]
    start = time.perf_counter()
    for query in ["-" * 12 + "x", "a" * 25 + "b", "-" * 30 + "z"]:
        assert rank(query, cands, now=NOW) == []
    assert time.perf_counter() - start < 1.0


def test_overlong_input_returns_nothing() -> None:
    cands = [Candidate("x" * 600)]
    assert rank("x" * 600, cands, now=NOW) == []
    assert rank("x" * 600, cands, now=NOW, config=RankingConfig(max_input_length=1000)) != []
