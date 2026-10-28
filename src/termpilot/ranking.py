"""Pure, deterministic suggestion ranking.

This module knows nothing about files, SQLite, or shells: it takes the typed
text plus candidate commands (with optional usage metadata) and returns the
best matches with an explanation of why they scored the way they did.

Score = w_match * match + w_recency * recency + w_frequency * frequency
        + w_context * context

but only for candidates whose ``match`` clears ``min_match``. That gate is
what stops a very frequent command from being suggested when it does not
actually resemble what was typed. All weights live in ``RankingConfig``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

# Match quality for each kind of match, in [0, 1]. Fuzzy matches scale up to
# FUZZY_MAX so that a prefix match always has a clear head start.
EXACT_PREFIX = 1.0
CASELESS_PREFIX = 0.9
TOKEN_PREFIX = 0.75
FUZZY_MAX = 0.6

_WORD_SEPARATORS = frozenset(" \t\n-_./:=")


@dataclass(frozen=True, slots=True)
class Candidate:
    """A command that could be suggested, plus optional usage metadata."""

    command: str
    count: int = 1
    last_used: float | None = None  # Unix epoch seconds
    dir_count: int = 0  # times run in the current directory (context)


@dataclass(frozen=True, slots=True)
class RankingConfig:
    weight_match: float = 0.55
    weight_recency: float = 0.20
    weight_frequency: float = 0.15
    weight_context: float = 0.10
    # Candidates whose match quality is below this are never suggested.
    min_match: float = 0.30
    # Recency halves every this many days.
    recency_half_life_days: float = 7.0
    # Counts at or above these saturate the frequency/context features.
    frequency_saturation: int = 100
    context_saturation: int = 20
    # Inputs shorter than this (after stripping) produce no suggestions.
    min_input_length: int = 2
    # Longer inputs (e.g. a pasted script) produce no suggestions.
    max_input_length: int = 512
    max_suggestions: int = 5


@dataclass(frozen=True, slots=True)
class Suggestion:
    command: str
    score: float
    match_kind: str
    features: dict[str, float] = field(default_factory=dict)


def _is_word_start(text: str, i: int) -> bool:
    return i == 0 or text[i - 1] in _WORD_SEPARATORS


def fuzzy_score(query: str, command: str) -> float:
    """Score a case-insensitive subsequence match in [0, 1] (0 = no match).

    Each matched character earns 1 point, +1 if it directly follows the
    previous matched character, +1 if it starts a word. The total is divided
    by the maximum possible (3 per character) and scaled by how much of the
    command the query covers. Several starting positions are tried because a
    greedy left-to-right scan can miss the better alignment.
    """
    q = query.lower()
    c = command.lower()
    if not q or len(q) > len(c):
        return 0.0
    best = 0
    starts = [i for i, ch in enumerate(c) if ch == q[0]][:8]
    for start in starts:
        points = 0
        prev = -2
        pos = start
        for qi, ch in enumerate(q):
            idx = c.find(ch, pos) if qi else start
            if idx < 0:
                points = -1
                break
            points += 1
            if idx == prev + 1:
                points += 1
            if _is_word_start(c, idx):
                points += 1
            prev = idx
            pos = idx + 1
        best = max(best, points)
    if best <= 0:
        return 0.0
    quality = best / (3 * len(q))
    coverage = len(q) / len(c)
    return quality * (0.75 + 0.25 * coverage)


def _token_prefix_match(query: str, command: str) -> bool:
    """True if each query token prefixes a later command token, in order.

    ``"g ch ma"`` matches ``"git checkout main"``.
    """
    q_tokens = query.lower().split()
    c_tokens = command.lower().split()
    if len(q_tokens) < 2:
        return False
    ci = 0
    for qt in q_tokens:
        while ci < len(c_tokens) and not c_tokens[ci].startswith(qt):
            ci += 1
        if ci == len(c_tokens):
            return False
        ci += 1
    return True


def match_quality(query: str, command: str) -> tuple[float, str]:
    """Return ``(quality, kind)`` describing how well ``command`` matches."""
    if command.startswith(query):
        return EXACT_PREFIX, "prefix"
    if command.lower().startswith(query.lower()):
        return CASELESS_PREFIX, "prefix-caseless"
    if _token_prefix_match(query, command):
        return TOKEN_PREFIX, "token-prefix"
    score = fuzzy_score(query, command)
    if score > 0:
        return FUZZY_MAX * score, "fuzzy"
    return 0.0, "none"


def recency_feature(last_used: float | None, now: float, half_life_days: float) -> float:
    if last_used is None:
        return 0.0
    age = max(0.0, now - last_used)
    return float(0.5 ** (age / (half_life_days * 86400)))


def _saturating_log(count: int, saturation: int) -> float:
    if count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(saturation))


def _sanitize(candidate: object) -> Candidate | None:
    """Coerce a possibly malformed candidate into a valid one, or drop it."""
    command = getattr(candidate, "command", None)
    if not isinstance(command, str) or not command.strip():
        return None

    def as_int(value: object, default: int) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            return default
        return max(0, int(value))

    last_used = getattr(candidate, "last_used", None)
    if isinstance(last_used, bool) or not isinstance(last_used, (int, float)):
        last_used = None
    elif not math.isfinite(last_used):
        last_used = None
    return Candidate(
        command=command,
        count=max(1, as_int(getattr(candidate, "count", 1), 1)),
        last_used=last_used,
        dir_count=as_int(getattr(candidate, "dir_count", 0), 0),
    )


def merge_duplicates(candidates: Iterable[object]) -> list[Candidate]:
    """Drop malformed candidates and merge duplicates of the same command."""
    merged: dict[str, Candidate] = {}
    for raw in candidates:
        cand = _sanitize(raw)
        if cand is None:
            continue
        prev = merged.get(cand.command)
        if prev is None:
            merged[cand.command] = cand
            continue
        last_values = [t for t in (prev.last_used, cand.last_used) if t is not None]
        merged[cand.command] = Candidate(
            command=cand.command,
            count=prev.count + cand.count,
            last_used=max(last_values) if last_values else None,
            dir_count=prev.dir_count + cand.dir_count,
        )
    return list(merged.values())


def _fuzzy_prefilter(query: str) -> re.Pattern[str]:
    """A cheap C-level "is ``query`` a subsequence of the command?" check.

    Use with ``.match()``. Each ``(?>.*?c)`` is an atomic group: it commits to
    the leftmost next occurrence of ``c`` and never backtracks into it.
    Leftmost choices are always optimal for subsequence matching, so this is
    exact and linear-time. (The naive ``a.*?b.*?c`` pattern backtracks
    catastrophically, e.g. ``"-"*12 + "x"`` against a long run of dashes.)
    """
    pattern = "".join(f"(?>.*?{re.escape(ch)})" for ch in query)
    return re.compile(pattern, re.IGNORECASE | re.DOTALL)


def rank(
    query: str,
    candidates: Iterable[object],
    *,
    now: float,
    config: RankingConfig | None = None,
    limit: int | None = None,
) -> list[Suggestion]:
    """Rank ``candidates`` against ``query``; best first.

    ``now`` is passed in (rather than read from the clock) so results are
    reproducible. Ties are broken by most recent use, then count, then
    alphabetically, so the output order is fully deterministic.
    """
    cfg = config or RankingConfig()
    limit = cfg.max_suggestions if limit is None else limit
    # Leading whitespace never matters to the shell; trailing whitespace does
    # ("git " means the user finished the word), so keep it.
    query = query.lstrip()
    stripped = query.strip()
    if limit <= 0 or not cfg.min_input_length <= len(stripped) <= cfg.max_input_length:
        return []
    prefilter = _fuzzy_prefilter(stripped)

    # Filter on the raw command string *before* validating/merging: profiling
    # showed per-candidate validation was over half the ranking time, and most
    # candidates fail this cheap C-level check anyway.
    plausible = (
        c
        for c in candidates
        if isinstance(command := getattr(c, "command", None), str) and prefilter.match(command)
    )
    scored: list[tuple[tuple[float, float, int, str], Suggestion]] = []
    for cand in merge_duplicates(plausible):
        quality, kind = match_quality(query, cand.command)
        if quality < cfg.min_match:
            continue
        features = {
            "match": quality,
            "recency": recency_feature(cand.last_used, now, cfg.recency_half_life_days),
            "frequency": _saturating_log(cand.count, cfg.frequency_saturation),
            "context": _saturating_log(cand.dir_count, cfg.context_saturation),
        }
        score = (
            cfg.weight_match * features["match"]
            + cfg.weight_recency * features["recency"]
            + cfg.weight_frequency * features["frequency"]
            + cfg.weight_context * features["context"]
        )
        score = round(score, 9)  # avoid float noise deciding ties
        key = (-score, -(cand.last_used or 0.0), -cand.count, cand.command)
        scored.append((key, Suggestion(cand.command, score, kind, features)))

    scored.sort(key=lambda item: item[0])
    return [s for _, s in scored[:limit]]


def pick_inline(query: str, suggestions: list[Suggestion]) -> Suggestion | None:
    """Choose the suggestion to show as inline ghost text.

    A suggestion identical to what is already typed adds nothing, so skip it.
    """
    for s in suggestions:
        if s.command != query.lstrip():
            return s
    return None
