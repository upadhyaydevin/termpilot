"""Command-line interface.

Exit codes:
  0  success
  1  no suggestion found (``suggest`` only), like grep
  2  usage error (argparse)
  3  configuration error
  4  history or data error (missing/unreadable files)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from termpilot import __version__
from termpilot.config import Config, ConfigError, load_config
from termpilot.history import HistoryError, iter_history
from termpilot.ranking import Candidate, pick_inline, rank

EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_CONFIG = 3
EXIT_DATA = 4


def _candidates_from_history(path: Path) -> list[Candidate]:
    counts: dict[str, int] = {}
    last: dict[str, int | None] = {}
    for entry in iter_history(path):
        counts[entry.command] = counts.get(entry.command, 0) + 1
        if entry.timestamp is not None:
            prev = last.get(entry.command)
            last[entry.command] = max(prev or 0, entry.timestamp)
        else:
            last.setdefault(entry.command, None)
    return [Candidate(cmd, n, last.get(cmd)) for cmd, n in counts.items()]


def cmd_suggest(args: argparse.Namespace, config: Config) -> int:
    candidates = _candidates_from_history(config.history_path)
    results = rank(args.text, candidates, now=time.time(), config=config.ranking)
    if args.format == "inline":
        best = pick_inline(args.text, results)
        if best is None:
            return EXIT_NO_MATCH
        sys.stdout.write(best.command)
        return EXIT_OK
    if not results:
        return EXIT_NO_MATCH
    if args.format == "json":
        payload = [
            {"command": s.command, "score": s.score, "match": s.match_kind, "features": s.features}
            for s in results
        ]
        print(json.dumps(payload, indent=2))
    else:
        for s in results:
            if args.explain:
                feats = " ".join(f"{k}={v:.2f}" for k, v in s.features.items())
                print(f"{s.score:.3f}  {s.command}    [{s.match_kind}; {feats}]")
            else:
                print(s.command)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="termpilot",
        description="Local-first command suggestions from your Zsh history.",
    )
    parser.add_argument("--version", action="version", version=f"termpilot {__version__}")
    parser.add_argument("--config", type=Path, help="config file (default: %(prog)s's XDG path)")
    parser.add_argument("--history", type=Path, help="zsh history file to read")
    parser.add_argument("--data-dir", type=Path, help="directory for the index database")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("suggest", help="print ranked suggestions for TEXT")
    p.add_argument("text", help="the partially typed command")
    p.add_argument("-n", "--max", type=int, dest="max_suggestions", help="number of results")
    p.add_argument("--explain", action="store_true", help="show scores and features")
    p.add_argument(
        "--format",
        choices=["text", "json", "inline"],
        default="text",
        help="'inline' prints only the single best completion (used by the zsh widget)",
    )
    p.set_defaults(func=cmd_suggest)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(
            args.config,
            history_path=args.history,
            data_dir=args.data_dir,
            max_suggestions=getattr(args, "max_suggestions", None),
        )
    except ConfigError as exc:
        print(f"termpilot: config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        return int(args.func(args, config))
    except HistoryError as exc:
        print(f"termpilot: {exc}", file=sys.stderr)
        return EXIT_DATA
