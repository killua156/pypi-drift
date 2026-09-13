"""Read the pin list, tolerating the shapes real CSVs arrive in.

Input tolerance lives here so the network path and the verdict logic never have
to think about headers, blank lines or ragged rows. A row that cannot be read is
not fatal: it becomes an error row carrying its line number, and the run goes on.
"""

from __future__ import annotations

import csv
import os
import stat
from typing import Iterable, List, Optional, Sequence, Tuple

from .models import STATUS_ERROR, Pin, Result

#: Accepted spellings for the package-name column of a header row.
NAME_HEADERS = frozenset({"package", "package_name", "packagename", "name"})
#: Accepted spellings for the version column of a header row.
VERSION_HEADERS = frozenset({"version", "pinned", "pinned_version", "pinnedversion"})

#: Rows whose first cell starts with this are notes, not data.
COMMENT_PREFIX = "#"


class CsvInputError(Exception):
    """The pin list itself could not be opened -- an operational failure."""


def read_pins(path: str) -> Tuple[List[Pin], List[Result]]:
    """Read ``path`` and return ``(pins, error_rows)``.

    Raises :class:`CsvInputError` only when the file cannot be read at all;
    per-row problems come back as error rows instead.
    """
    if not os.path.exists(path):
        raise CsvInputError("CSV file not found: {}".format(path))
    _reject_unreadable_kind(path)
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as handle:
            return parse_rows(handle)
    except UnicodeDecodeError:
        raise CsvInputError("CSV file is not valid UTF-8 text: {}".format(path))
    except OSError as exc:
        raise CsvInputError("could not read CSV file {}: {}".format(path, exc.strerror or exc))


def _reject_unreadable_kind(path: str) -> None:
    """Refuse paths that can never hold a pin list, and let the rest through.

    FIFOs and character devices are allowed on purpose: that is how process
    substitution (``pypi-drift <(grep django pins.csv)``) and ``/dev/stdin``
    arrive. A FIFO with no writer blocks, but so does ``cat`` on one -- that is
    standard behavior, not a bug to guard against.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        raise CsvInputError(
            "could not read CSV file {}: {}".format(path, exc.strerror or exc)
        )
    if stat.S_ISDIR(mode):
        raise CsvInputError("expected a CSV file but found a directory: {}".format(path))
    if not (stat.S_ISREG(mode) or stat.S_ISFIFO(mode) or stat.S_ISCHR(mode)):
        raise CsvInputError("not a readable file: {}".format(path))


def parse_rows(lines: Iterable[str]) -> Tuple[List[Pin], List[Result]]:
    """Parse an iterable of CSV lines (a file handle, or a list of strings)."""
    pins: List[Pin] = []
    errors: List[Result] = []
    name_index, version_index = 0, 1
    header_checked = False
    previous_line_num = 0

    reader = csv.reader(lines)
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            errors.append(
                _error_row("", "line {}: malformed CSV: {}".format(reader.line_num, exc),
                           reader.line_num)
            )
            previous_line_num = reader.line_num
            continue

        line_num = reader.line_num
        start_line = previous_line_num + 1
        previous_line_num = line_num

        if _spans_lines(row):
            errors.append(_unterminated_quote_row(start_line, line_num))
            continue

        if _is_blank(row) or _is_comment(row):
            continue

        if not header_checked:
            header_checked = True
            indices = _header_indices(row)
            if indices is not None:
                name_index, version_index = indices
                continue

        pin_or_error = _row_to_pin(row, name_index, version_index, line_num)
        if isinstance(pin_or_error, Pin):
            pins.append(pin_or_error)
        else:
            errors.append(pin_or_error)

    return pins, errors


def _spans_lines(row: Sequence[str]) -> bool:
    """True when a parsed cell swallowed a line break.

    Neither a package name nor a version ever contains one, so this always means
    an unterminated quote has eaten the rows that followed it.
    """
    return any("\n" in cell or "\r" in cell for cell in row)


def _unterminated_quote_row(start_line: int, end_line: int) -> Result:
    """Report the swallowed range, so no package disappears without a report line."""
    if end_line > start_line:
        message = (
            "line {}: unterminated quote swallowed lines {}-{}; "
            "those packages were not checked".format(start_line, start_line, end_line)
        )
    else:
        message = "line {}: unterminated quote".format(start_line)
    return _error_row("", message, start_line)


def _is_blank(row: Sequence[str]) -> bool:
    return not row or all(not cell.strip() for cell in row)


def _is_comment(row: Sequence[str]) -> bool:
    return bool(row) and row[0].lstrip().startswith(COMMENT_PREFIX)


def _header_indices(row: Sequence[str]) -> Optional[Tuple[int, int]]:
    """Return ``(name_index, version_index)`` when ``row`` is a header, else ``None``.

    Both columns must be recognised, so a package genuinely called ``name`` with
    a version of ``1.0`` is still read as data.
    """
    cells = [cell.strip().lower() for cell in row]
    name_index = version_index = None
    for index, cell in enumerate(cells):
        if name_index is None and cell in NAME_HEADERS:
            name_index = index
        elif version_index is None and cell in VERSION_HEADERS:
            version_index = index
    if name_index is None or version_index is None:
        return None
    return name_index, version_index


def _row_to_pin(row: Sequence[str], name_index: int, version_index: int, line_num: int):
    """Turn one data row into a :class:`Pin`, or an error :class:`Result`."""
    needed = max(name_index, version_index) + 1
    if len(row) < needed:
        return _error_row(
            row[name_index].strip() if len(row) > name_index else "",
            "line {}: expected at least {} columns (package,version), got {}".format(
                line_num, needed, len(row)
            ),
            line_num,
        )

    name = row[name_index].strip()
    version = row[version_index].strip()
    if not name:
        return _error_row("", "line {}: missing package name".format(line_num), line_num)
    if not version:
        return _error_row(name, "line {}: missing pinned version".format(line_num), line_num)
    return Pin(name=name, version=version, line=line_num)


def _error_row(name: str, message: str, line_num: int) -> Result:
    return Result(name=name, status=STATUS_ERROR, message=message, line=line_num)
