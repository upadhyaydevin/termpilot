"""Command-line interface.

Exit codes:
  0  success
  1  no suggestion found (``suggest`` only), like grep
  2  usage error (argparse)
  3  configuration error
  4  history or data error (missing/unreadable files, unusable index);
     for ``doctor``: at least one check failed
  70 unexpected internal error (rerun with ``--debug`` for a traceback)

Imports used only by rarely-run commands (doctor, install-zsh, JSON output)
live inside those functions: the zsh widget starts this program on every
keystroke, and module imports were measured as the largest share of its
latency (see docs/design-decisions.md).
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from termpilot import __version__
from termpilot.config import Config, ConfigError, load_config
from termpilot.errors import TermPilotError
from termpilot.history import HistoryError, check_readable
from termpilot.ranking import pick_inline, rank
from termpilot.storage import (
    SCHEMA_VERSION,
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
EXIT_INTERNAL = 70


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
        import json

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


def _data_files(config: Config) -> list[Path]:
    """Every file TermPilot itself creates (never the user's history)."""
    db = config.db_path
    files = [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")]
    files.append(config.context_log_path)
    if config.data_dir.is_dir():
        files.extend(sorted(config.data_dir.glob(db.name + ".corrupt-*")))
    return [f for f in files if f.exists()]


def _confirm(question: str, assume_yes: bool) -> bool | None:
    """True/False from the user; None if we cannot ask (no terminal, no --yes)."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return None
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_purge(args: argparse.Namespace, config: Config) -> int:
    files = _data_files(config)
    if not files:
        print(f"nothing to delete in {config.data_dir}")
        return EXIT_OK
    print("This deletes TermPilot's own data (your zsh history file is not touched):")
    for f in files:
        print(f"  {f}")
    answer = _confirm("Delete these files?", args.yes)
    if answer is None:
        print(
            "termpilot: refusing to delete without --yes (stdin is not a terminal)",
            file=sys.stderr,
        )
        return EXIT_DATA
    if not answer:
        print("aborted")
        return EXIT_OK
    for f in files:
        f.unlink()
    print(f"deleted {len(files)} file(s)")
    return EXIT_OK


def cmd_paths(args: argparse.Namespace, config: Config) -> int:
    """Machine-readable key=value paths (read by the zsh integration)."""
    from termpilot.integration import zsh_script_path

    print(f"config={config.config_path or ''}")
    print(f"history={config.history_path}")
    print(f"data_dir={config.data_dir}")
    print(f"database={config.db_path}")
    print(f"context_log={config.context_log_path if config.context_enabled else ''}")
    print(f"zsh_script={zsh_script_path()}")
    return EXIT_OK


def cmd_init(args: argparse.Namespace, config: Config) -> int:
    from termpilot.integration import snippet

    print("# Add these lines to your ~/.zshrc (or run `termpilot install-zsh`):")
    print(snippet(), end="")
    return EXIT_OK


def cmd_install_zsh(args: argparse.Namespace, config: Config) -> int:
    from termpilot.integration import default_zshrc, install, is_installed, snippet

    zshrc = args.zshrc or default_zshrc()
    if is_installed(zshrc):
        print(f"already installed in {zshrc.resolve()}")
        return EXIT_OK
    print(f"This appends the following block to {zshrc.resolve()} (a backup is made first):\n")
    print(snippet())
    answer = _confirm("Proceed?", args.yes)
    if answer is None:
        print(
            "termpilot: refusing to edit without --yes (stdin is not a terminal)", file=sys.stderr
        )
        return EXIT_DATA
    if not answer:
        print("aborted; nothing changed")
        return EXIT_OK
    result = install(zshrc)
    if result.backup:
        print(f"backup: {result.backup}")
    print("installed. Open a new terminal (or run `exec zsh`) to start using TermPilot.")
    if not config.db_path.exists():
        print("tip: run `termpilot index` once now to import your existing history.")
    return EXIT_OK


def cmd_uninstall_zsh(args: argparse.Namespace, config: Config) -> int:
    from termpilot.integration import default_zshrc, is_installed, uninstall

    zshrc = args.zshrc or default_zshrc()
    if not is_installed(zshrc):
        print(f"no TermPilot block found in {zshrc.resolve()}; nothing to do")
        return EXIT_OK
    answer = _confirm(f"Remove the TermPilot block from {zshrc.resolve()}?", args.yes)
    if answer is None:
        print(
            "termpilot: refusing to edit without --yes (stdin is not a terminal)", file=sys.stderr
        )
        return EXIT_DATA
    if not answer:
        print("aborted; nothing changed")
        return EXIT_OK
    result = uninstall(zshrc)
    if result.backup:
        print(f"backup: {result.backup}")
    print("removed. Open a new terminal for it to take effect.")
    print("To also delete TermPilot's index and context log: termpilot purge")
    return EXIT_OK


def cmd_benchmark(args: argparse.Namespace, config: Config) -> int:
    from termpilot.benchmark import format_report, run_benchmark, save_json

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    report = run_benchmark(
        args.sizes,
        queries=args.queries,
        process_runs=args.runs,
        seed=args.seed,
        log=log,
    )
    text = format_report(report)
    print(text)
    if args.json:
        save_json(report, args.json)
    if args.markdown:
        args.markdown.write_text(text + "\n", encoding="utf-8")
    return EXIT_OK


class _Report:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, label: str, detail: str) -> None:
        print(f"  ok    {label:<14} {detail}")

    def warn(self, label: str, detail: str) -> None:
        print(f"  warn  {label:<14} {detail}")

    def fail(self, label: str, detail: str) -> None:
        self.failures += 1
        print(f"  FAIL  {label:<14} {detail}")


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def cmd_doctor(args: argparse.Namespace, config: Config) -> int:
    """Check the environment. Reports sizes and counts, never command text."""
    import platform
    import shutil

    from termpilot.integration import IntegrationError, default_zshrc, is_installed

    r = _Report()
    print(f"termpilot {__version__}")

    py = sys.version_info
    detail = f"{platform.python_version()} ({sys.executable})"
    (r.ok if py >= (3, 11) else r.fail)("python", detail)
    r.ok("platform", f"{platform.system()} {platform.release()}")
    if platform.system() != "Darwin":
        r.warn("platform", "only macOS is tested; Linux should work but is unverified")

    zsh = shutil.which("zsh")
    (r.ok if zsh else r.warn)("zsh", zsh or "not found on PATH (needed for the integration)")
    exe = shutil.which("termpilot")
    (r.ok if exe else r.warn)(
        "executable",
        exe or "`termpilot` is not on PATH; the zsh widget needs it (or set TERMPILOT_BIN)",
    )

    if config.config_path:
        r.ok("config", f"{config.config_path} (valid)")
    else:
        r.ok("config", "no config file; using defaults")

    try:
        check_readable(config.history_path, "history file")
        r.ok("history", f"{config.history_path} ({config.history_path.stat().st_size} bytes)")
    except HistoryError as exc:
        r.fail("history", f"{exc}. Set history_path in the config or pass --history.")

    db = config.db_path
    if not db.exists():
        r.fail("index", f"{db} does not exist; run `termpilot index`")
    else:
        try:
            conn = open_readonly(db)
            try:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                stats = index_stats(conn)
            finally:
                conn.close()
            if check != "ok":
                r.fail("index", "integrity check failed; run `termpilot index --rebuild`")
            else:
                r.ok(
                    "index",
                    f"{db} (schema v{SCHEMA_VERSION}, {stats.commands} commands, "
                    f"{stats.directories} directories)",
                )
            if _mode(db) != "0o600":
                r.warn("index", f"permissions are {_mode(db)}; expected 0o600")
        except (StorageError, sqlite3.Error) as exc:
            r.fail("index", str(exc))

    log = config.context_log_path
    if not config.context_enabled:
        r.ok("context", "disabled in config")
    elif log.exists():
        r.ok("context", f"{log} ({log.stat().st_size} bytes, mode {_mode(log)})")
        if _mode(log) != "0o600":
            r.warn("context", f"permissions are {_mode(log)}; expected 0o600")
    else:
        r.ok("context", "no context log yet (created once the zsh integration runs)")

    try:
        zshrc = default_zshrc()
        if is_installed(zshrc):
            r.ok("zsh setup", f"TermPilot block found in {zshrc}")
        else:
            r.warn("zsh setup", f"not enabled in {zshrc}; see `termpilot init zsh`")
    except IntegrationError as exc:
        r.warn("zsh setup", str(exc))

    print("all checks passed" if not r.failures else f"{r.failures} check(s) failed")
    return EXIT_OK if not r.failures else EXIT_DATA


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="termpilot",
        description="Local-first command suggestions from your Zsh history.",
    )
    parser.add_argument("--version", action="version", version=f"termpilot {__version__}")
    parser.add_argument("--config", type=Path, help="config file (default: %(prog)s's XDG path)")
    parser.add_argument("--history", type=Path, help="zsh history file to read")
    parser.add_argument("--data-dir", type=Path, help="directory for the index database")
    parser.add_argument(
        "--debug", action="store_true", help="show tracebacks for unexpected errors"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("index", help="import new history into the index")
    p.add_argument("--rebuild", action="store_true", help="discard the index and re-import")
    p.add_argument("-q", "--quiet", action="store_true", help="print nothing on success")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("stats", help="show index size (counts only, no commands)")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("doctor", help="check configuration, history, index and shell setup")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("paths", help="print file locations as key=value lines")
    p.set_defaults(func=cmd_paths)

    p = sub.add_parser("init", help="print the snippet that enables TermPilot in your shell")
    p.add_argument("shell", choices=["zsh"])
    p.set_defaults(func=cmd_init)

    for name, func, verb in (
        ("install-zsh", cmd_install_zsh, "add TermPilot to"),
        ("uninstall-zsh", cmd_uninstall_zsh, "remove TermPilot from"),
    ):
        p = sub.add_parser(name, help=f"{verb} your .zshrc (asks first; makes a backup)")
        p.add_argument("--zshrc", type=Path, help="file to edit (default: $ZDOTDIR/.zshrc)")
        p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
        p.set_defaults(func=func)

    p = sub.add_parser("benchmark", help="measure latency on synthetic history (no real data)")
    p.add_argument(
        "--sizes", type=_positive_int, nargs="+", default=[1000, 10000, 100000],
        help="history sizes to test (default: 1000 10000 100000)",
    )  # fmt: skip
    p.add_argument("--queries", type=_positive_int, default=200, help="queries per size")
    p.add_argument("--runs", type=int, default=30, help="subprocess runs per size (0 to skip)")
    p.add_argument("--seed", type=int, default=42, help="random seed for the dataset")
    p.add_argument("--json", type=Path, help="also write raw results as JSON")
    p.add_argument("--markdown", type=Path, help="also write the report as Markdown")
    p.add_argument("-q", "--quiet", action="store_true", help="no progress messages")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("purge", help="delete the index and context log (not your history)")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_purge)

    p = sub.add_parser("suggest", help="print ranked suggestions for TEXT")
    p.add_argument("text", help="the partially typed command")
    p.add_argument(
        "-n", "--max", type=_positive_int, dest="max_suggestions", help="number of results"
    )
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
    except TermPilotError as exc:
        print(f"termpilot: {exc}", file=sys.stderr)
        return EXIT_DATA
    except sqlite3.Error as exc:
        print(
            f"termpilot: database error: {exc}; try `termpilot index --rebuild`", file=sys.stderr
        )
        return EXIT_DATA
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # last resort: concise message, details on request
        if args.debug:
            raise
        print(
            f"termpilot: unexpected error: {type(exc).__name__}: {exc} "
            "(rerun with --debug for details)",
            file=sys.stderr,
        )
        return EXIT_INTERNAL
