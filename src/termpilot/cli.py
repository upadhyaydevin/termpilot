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
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from termpilot import __version__
from termpilot.config import Config, ConfigError, load_config
from termpilot.history import HistoryError
from termpilot.ranking import pick_inline, rank
from termpilot.storage import (
    StorageError,
    fetch_candidates,
    index_stats,
    open_readonly,
    open_writable,
    rebuild,
    refresh,
)

EXIT_OK = 0
EXIT_NO_MATCH = 1
EXIT_CONFIG = 3
EXIT_DATA = 4


def _current_dir() -> str | None:
    try:
        return os.getcwd()
    except OSError:  # the directory was deleted out from under the shell
        return None


def cmd_index(args: argparse.Namespace, config: Config) -> int:
    conn, recovered = open_writable(config.db_path)
    try:
        if recovered is not None:
            print(f"termpilot: index was corrupt; moved it to {recovered}", file=sys.stderr)
        if args.rebuild:
            rebuild(conn)
        context = config.context_log_path if config.context_enabled else None
        result = refresh(conn, config.history_path, context)
        stats = index_stats(conn)
    finally:
        conn.close()
    if not args.quiet:
        h, c = result.history, result.context
        print(f"history:  {h.mode}, {h.entries} new entries, {h.stats.skipped} skipped")
        print(f"context:  {c.mode}, {c.entries} new entries, {c.stats.skipped} skipped")
        print(f"index:    {stats.commands} distinct commands, {stats.directories} directories")
    return EXIT_OK


def cmd_stats(args: argparse.Namespace, config: Config) -> int:
    conn = open_readonly(config.db_path)
    try:
        stats = index_stats(conn)
    finally:
        conn.close()
    print(f"commands:     {stats.commands}")
    print(f"directories:  {stats.directories}")
    print(f"database:     {config.db_path} ({config.db_path.stat().st_size} bytes)")
    return EXIT_OK


def cmd_suggest(args: argparse.Namespace, config: Config) -> int:
    cwd = (args.cwd or _current_dir()) if config.context_enabled else None
    conn = open_readonly(config.db_path)
    try:
        candidates = fetch_candidates(conn, args.text, cwd)
    finally:
        conn.close()
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

    p = sub.add_parser("index", help="import new history into the index")
    p.add_argument("--rebuild", action="store_true", help="discard the index and re-import")
    p.add_argument("-q", "--quiet", action="store_true", help="print nothing on success")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("stats", help="show index size (counts only, no commands)")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("suggest", help="print ranked suggestions for TEXT")
    p.add_argument("text", help="the partially typed command")
    p.add_argument("-n", "--max", type=int, dest="max_suggestions", help="number of results")
    p.add_argument("--explain", action="store_true", help="show scores and features")
    p.add_argument("--cwd", help="directory to use for context (default: current)")
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
    except (HistoryError, StorageError) as exc:
        print(f"termpilot: {exc}", file=sys.stderr)
        return EXIT_DATA
