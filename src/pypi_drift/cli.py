"""Command line entry point: arguments, rendering, exit codes.

The only module that touches stdout or the process exit status -- the web UI
renders into a stream handed to it from here. Everything printed is assembled
from :class:`~pypi_drift.models.Result` rows, so the table, the JSON document
and the page always report the same verdicts.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from typing import List, Optional, Sequence, TextIO

from . import __version__, pypi
from .compare import evaluate, fetch_error_result, invalid_pin_result, parse_version
from .csv_input import CsvInputError, read_pins
from .models import (
    STATUS_ERROR,
    STATUS_FLAGGED,
    Pin,
    Result,
    Summary,
    build_document,
    dump_document,
)

#: Every package is current (or the run was forced clean with --exit-zero).
EXIT_OK = 0
#: At least one package is a major version behind.
EXIT_FLAGGED = 1
#: Operational failure: missing CSV, bad arguments, an interrupted run.
EXIT_ERROR = 2
#: Something could not be checked at all, and nothing was flagged. An
#: incomplete report must never read in CI as "all pins current".
EXIT_UNCHECKED = 3

#: More threads than this is never useful and starts to cost connections.
MAX_WORKERS = 64

_COLUMNS = ("STATUS", "PACKAGE", "PINNED", "LATEST", "MAJOR", "NOTE")

#: The one word routed away from the CSV parser, before it sees a positional.
SERVE_COMMAND = "serve"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pypi-drift",
        description=(
            "Check a CSV of pinned Python dependencies against PyPI and report "
            "the pins that are at least one major version behind."
        ),
        epilog=(
            "Exit codes: 0 clean, 1 something flagged, 2 operational failure, "
            "3 something could not be checked. Flagged wins when a run has both. "
            "Run 'pypi-drift serve' for the same check as a local web page."
        ),
    )
    parser.add_argument("csv", help="path to a CSV of package,pinned_version rows")
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="emit a single JSON document instead of a table",
    )
    parser.add_argument(
        "--only-flagged",
        action="store_true",
        help="list only the drifted packages (the summary still counts them all)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=pypi.DEFAULT_TIMEOUT,
        help="per-request timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=pypi.DEFAULT_WORKERS,
        help="maximum concurrent requests, 1-{} (default: %(default)s)".format(MAX_WORKERS),
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help=(
            "exit 0 even when packages are flagged or unchecked "
            "(report-only use); operational failures still exit 2"
        ),
    )
    parser.add_argument("--version", action="version", version="pypi-drift {}".format(__version__))
    return parser


def build_serve_parser() -> argparse.ArgumentParser:
    """Flags for ``pypi-drift serve``. Deliberately no host or bind option.

    The web module is imported here rather than at module scope so a plain
    ``pypi-drift pins.csv`` never pays for ``http.server``.
    """
    from .web import DEFAULT_PORT

    parser = argparse.ArgumentParser(
        prog="pypi-drift serve",
        description=(
            "Serve the drift checker as a local web page: paste a pin list in "
            "the browser and get the same table back."
        ),
        epilog=(
            "The server binds 127.0.0.1 only and is reachable from this machine "
            "alone. Nothing is written to disk."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="preferred port; the next free one is used if it is busy (default: %(default)s)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser window; just print the URL",
    )
    return parser


def check(
    pins: Sequence[Pin],
    timeout: float = pypi.DEFAULT_TIMEOUT,
    workers: int = pypi.DEFAULT_WORKERS,
) -> List[Result]:
    """Resolve every pin into a verdict.

    Pins whose version is not PEP 440 become error rows without issuing a
    request; the rest are looked up concurrently.
    """
    results: List[Result] = []
    to_fetch: List[Pin] = []
    for pin in pins:
        if parse_version(pin.version) is None:
            results.append(invalid_pin_result(pin))
        else:
            to_fetch.append(pin)

    outcomes = pypi.fetch_all(
        (pin.name for pin in to_fetch), timeout=timeout, workers=workers
    )
    for pin in to_fetch:
        outcome = outcomes.get(pypi.normalize_name(pin.name))
        if outcome is None or not outcome.ok:
            reason = outcome.error if outcome is not None else "no response from PyPI"
            results.append(fetch_error_result(pin, reason or "no response from PyPI"))
        else:
            results.append(evaluate(pin, outcome.version))
    return results


def render_table(results: Sequence[Result], stream: TextIO) -> None:
    """Print an aligned table. Prints nothing at all when there are no rows."""
    if not results:
        return
    rows = [_row_cells(result) for result in results]
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(_COLUMNS)
    ]
    for cells in [list(_COLUMNS)] + rows:
        stream.write(_format_line(cells, widths) + "\n")


def _row_cells(result: Result) -> List[str]:
    if result.status == STATUS_FLAGGED:
        major = "{} -> {}".format(result.pinned_major, result.latest_major)
    elif result.status == STATUS_ERROR:
        major = "-"
    else:
        major = result.pinned_major or "-"
    return [
        _flatten(result.status.upper()),
        _flatten(result.name) or "(unnamed)",
        _flatten(result.pinned) or "-",
        _flatten(result.latest) or "-",
        _flatten(major) or "-",
        _flatten(result.message),
    ]


def _flatten(text: Optional[str]) -> str:
    """Collapse any whitespace to single spaces, so a cell stays on one line."""
    return " ".join(str(text).split()) if text else ""


def _format_line(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = [
        cell.ljust(widths[index]) if index < len(widths) - 1 else cell
        for index, cell in enumerate(cells)
    ]
    return "  ".join(padded).rstrip()


def render_summary(summary: Summary, stream: TextIO) -> None:
    stream.write(
        "Checked {} package{}: {} flagged, {} ok, {} error{}.\n".format(
            summary.checked,
            "" if summary.checked == 1 else "s",
            summary.flagged,
            summary.ok,
            summary.errors,
            "" if summary.errors == 1 else "s",
        )
    )


def render_json(results: Sequence[Result], summary: Summary, stream: TextIO) -> None:
    dump_document(build_document(summary, results), stream)


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # `csv` is positional, so the existing parser would read a bare `serve` as a
    # file path. Route it first; everything else parses exactly as it always has.
    if raw and raw[0] == SERVE_COMMAND:
        return _serve(raw[1:])

    parser = build_parser()
    args = parser.parse_args(raw)

    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite number greater than 0")
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error("--workers must be between 1 and {}".format(MAX_WORKERS))

    try:
        return _run(args)
    except BrokenPipeError:
        return _exit_on_broken_pipe()
    except KeyboardInterrupt:
        sys.stderr.write("pypi-drift: interrupted\n")
        return EXIT_ERROR
    except Exception as exc:  # never a traceback; always one line and exit 2
        sys.stderr.write("pypi-drift: {}: {}\n".format(exc.__class__.__name__, exc))
        return EXIT_ERROR


def _serve(argv: Sequence[str]) -> int:
    """Run ``pypi-drift serve``. Owns its own exit codes, like :func:`main`."""
    parser = build_serve_parser()
    args = parser.parse_args(argv)

    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    from . import web

    try:
        web.serve(port=args.port, open_browser=not args.no_browser, stream=sys.stdout)
    except KeyboardInterrupt:  # raised before serve_forever installs its own guard
        return EXIT_OK
    except BrokenPipeError:  # `pypi-drift serve | head`, same as the CSV path
        return _exit_on_broken_pipe()
    except OSError as exc:
        sys.stderr.write("pypi-drift: {}\n".format(exc))
        return EXIT_ERROR
    except Exception as exc:  # never a traceback; always one line and exit 2
        sys.stderr.write("pypi-drift: {}: {}\n".format(exc.__class__.__name__, exc))
        return EXIT_ERROR
    return EXIT_OK


def _exit_on_broken_pipe() -> int:
    """Exit quietly when the reader goes away (``pypi-drift pins.csv | head``).

    stdout is redirected to devnull first so the interpreter does not print
    "Exception ignored" noise when it flushes the dead pipe at shutdown.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        # A substituted stdout may have no real fd; there is nothing to salvage.
        pass
    return EXIT_OK


def _run(args: argparse.Namespace) -> int:
    """Do the work. Raises; :func:`main` owns every exit code."""
    try:
        pins, parse_errors = read_pins(args.csv)
    except CsvInputError as exc:
        sys.stderr.write("pypi-drift: {}\n".format(exc))
        return EXIT_ERROR

    results = check(pins, timeout=args.timeout, workers=args.workers)
    results.extend(parse_errors)
    results.sort(key=lambda result: (result.line, result.name))

    summary = Summary.from_results(results)
    shown = [r for r in results if r.is_flagged] if args.only_flagged else list(results)

    if args.as_json:
        render_json(shown, summary, sys.stdout)
    else:
        render_table(shown, sys.stdout)
        render_summary(summary, sys.stdout)

    # Force a dead pipe to raise here, where main catches it, not at shutdown.
    sys.stdout.flush()

    if args.exit_zero:
        return EXIT_OK
    if summary.flagged:
        return EXIT_FLAGGED  # takes precedence over unchecked rows
    if summary.errors:
        return EXIT_UNCHECKED
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
