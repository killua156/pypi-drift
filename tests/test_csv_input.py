"""Input tolerance: headers, positional rows, blanks, comments, ragged rows."""

import csv
import os
import shutil
import signal
import socket
import tempfile
import threading

import pytest

from pypi_drift.csv_input import CsvInputError, parse_rows, read_pins
from pypi_drift.models import STATUS_ERROR


def parse(text):
    return parse_rows(text.splitlines(keepends=True))


def test_header_row_selects_columns_by_name():
    pins, errors = parse("package,version\nrequests,2.31.0\n")
    assert errors == []
    assert [(p.name, p.version) for p in pins] == [("requests", "2.31.0")]


@pytest.mark.parametrize(
    "header",
    [
        "package,version",
        "PACKAGE,VERSION",
        "Package,Version",
        "name,pinned",
        "NAME,PINNED_VERSION",
        "package_name,pinned",
        "  package , version  ",
    ],
)
def test_header_spellings_are_all_recognised(header):
    pins, errors = parse(header + "\nrequests,2.31.0\n")
    assert errors == []
    assert [(p.name, p.version) for p in pins] == [("requests", "2.31.0")]


def test_header_columns_may_be_in_either_order():
    pins, _ = parse("version,package\n2.31.0,requests\n")
    assert [(p.name, p.version) for p in pins] == [("requests", "2.31.0")]


def test_without_a_header_columns_are_positional():
    pins, errors = parse("requests,2.31.0\nflask,1.1.4\n")
    assert errors == []
    assert [(p.name, p.version) for p in pins] == [
        ("requests", "2.31.0"),
        ("flask", "1.1.4"),
    ]


def test_a_package_literally_called_name_is_still_data():
    """Both header cells must be recognised, so `name,1.0` is a pin, not a header."""
    pins, _ = parse("name,1.0\n")
    assert [(p.name, p.version) for p in pins] == [("name", "1.0")]


def test_blank_and_comment_rows_are_skipped_silently():
    pins, errors = parse(
        "package,version\n"
        "\n"
        "# a note\n"
        "requests,2.31.0\n"
        "   \n"
        "  # indented note\n"
        ",,\n"
        "flask,1.1.4\n"
    )
    assert errors == []
    assert [p.name for p in pins] == ["requests", "flask"]


def test_short_row_is_an_error_row_citing_its_line():
    pins, errors = parse("package,version\nrequests,2.31.0\nlonely\n")
    assert [p.name for p in pins] == ["requests"]
    assert len(errors) == 1
    assert errors[0].status == STATUS_ERROR
    assert errors[0].line == 3
    assert "line 3" in errors[0].message


def test_missing_name_or_version_cell_is_an_error_row():
    _, errors = parse(",2.0.0\nrequests,\n")
    assert [e.line for e in errors] == [1, 2]
    assert "missing package name" in errors[0].message
    assert "missing pinned version" in errors[1].message


def test_extra_columns_are_ignored():
    pins, errors = parse("requests,2.31.0,some note,another\n")
    assert errors == []
    assert (pins[0].name, pins[0].version) == ("requests", "2.31.0")


def test_line_numbers_survive_a_header_and_blank_lines():
    pins, _ = parse("package,version\n\nrequests,2.31.0\n")
    assert pins[0].line == 3


def test_whitespace_padded_and_quoted_cells_are_trimmed():
    pins, _ = parse('  requests  ,  2.31.0  \n')
    assert (pins[0].name, pins[0].version) == ("requests", "2.31.0")
    quoted, _ = parse('"requests","  2.31.0  "\n')
    assert (quoted[0].name, quoted[0].version) == ("requests", "2.31.0")
    commas, _ = parse('"requests, the library",2.31.0\n')
    assert commas[0].name == "requests, the library"


def test_original_spelling_is_preserved():
    pins, _ = parse("Flask_SQLAlchemy,2.5.1\n")
    assert pins[0].name == "Flask_SQLAlchemy"


def test_empty_file_yields_nothing(write_csv):
    pins, errors = read_pins(write_csv(""))
    assert (pins, errors) == ([], [])


def test_reads_a_real_file_and_strips_a_bom(write_csv):
    path = write_csv("﻿package,version\nrequests,2.31.0\n")
    pins, errors = read_pins(path)
    assert errors == []
    assert pins[0].name == "requests"


def test_missing_file_raises_with_the_path_named(tmp_path):
    missing = str(tmp_path / "missing.csv")
    with pytest.raises(CsvInputError) as excinfo:
        read_pins(missing)
    assert missing in str(excinfo.value)


def test_directory_instead_of_file_raises(tmp_path):
    with pytest.raises(CsvInputError):
        read_pins(str(tmp_path))


def test_an_unterminated_quote_is_reported_not_silently_swallowed():
    """The quote eats the rows after it; those packages must not vanish unreported."""
    pins, errors = parse(
        "requests,2.31.0\n"
        '"broken,1.0.0\n'
        "flask,1.1.4\n"
        "six,1.16.0\n"
    )
    assert [p.name for p in pins] == ["requests"]
    assert len(errors) == 1
    assert errors[0].status == STATUS_ERROR
    assert errors[0].line == 2
    assert "unterminated quote" in errors[0].message
    assert "2-4" in errors[0].message, "the swallowed range must be named"


def test_an_unterminated_quote_on_the_last_line_is_still_reported():
    pins, errors = parse('requests,2.31.0\n"broken,1.0.0\n')
    assert [p.name for p in pins] == ["requests"]
    assert "unterminated quote" in errors[0].message
    assert errors[0].line == 2


def test_an_oversized_field_is_an_error_row_and_the_good_rows_survive():
    """csv.reader rejects a field past its size limit; that must not crash the run."""
    original_limit = csv.field_size_limit()
    csv.field_size_limit(64)
    try:
        pins, errors = parse(
            "requests,2.31.0\n"
            'huge,"{}"\n'.format("A" * 500)
            + "flask,1.1.4\n"
        )
    finally:
        csv.field_size_limit(original_limit)

    assert [p.name for p in pins] == ["requests", "flask"], "good rows must survive"
    assert len(errors) == 1
    assert errors[0].status == STATUS_ERROR
    assert errors[0].line == 2
    assert "line 2" in errors[0].message and "malformed CSV" in errors[0].message


def test_a_non_utf8_file_raises_rather_than_tracebacking(tmp_path):
    """An Excel-exported CSV is often cp1252, not UTF-8."""
    path = tmp_path / "latin1.csv"
    path.write_bytes(b"package,version\ncaf\xe9,1.0.0\n")
    with pytest.raises(CsvInputError) as excinfo:
        read_pins(str(path))
    assert "UTF-8" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="POSIX alarm needed")
def test_a_fifo_with_a_writer_is_read_like_any_other_file(tmp_path):
    """Process substitution -- `pypi-drift <(grep django pins.csv)` -- arrives as a FIFO.

    The alarm keeps a regression here a fast failure rather than a hung suite.
    """
    fifo = tmp_path / "pipe.csv"
    os.mkfifo(str(fifo))

    def feed():
        # Blocks until the reader opens the other end; the two rendezvous.
        with open(str(fifo), "w", encoding="utf-8") as handle:
            handle.write("package,version\nrequests,2.31.0\nflask,1.1.4\n")

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()

    def timed_out(signum, frame):
        raise AssertionError("read_pins() never completed on a FIFO with a writer")

    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(10)
    try:
        pins, errors = read_pins(str(fifo))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    writer.join(timeout=5)

    assert errors == []
    assert [(p.name, p.version) for p in pins] == [
        ("requests", "2.31.0"),
        ("flask", "1.1.4"),
    ]


def test_a_character_device_is_read_not_refused():
    """/dev/stdin and /dev/fd/N are character devices on some platforms."""
    assert read_pins("/dev/null") == ([], [])


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX needed")
def test_a_socket_is_refused_as_unreadable():
    """Nothing but a file, FIFO or character device can hold a pin list."""
    # AF_UNIX paths are capped near 104 bytes, so pytest's tmp_path is too long.
    directory = tempfile.mkdtemp(dir="/tmp")
    path = os.path.join(directory, "s")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(path)
        with pytest.raises(CsvInputError) as excinfo:
            read_pins(path)
        # The specific message, not just any CsvInputError: open() would also
        # fail on a socket, so a looser assertion would not pin this branch.
        assert str(excinfo.value) == "not a readable file: {}".format(path)
    finally:
        sock.close()
        shutil.rmtree(directory, ignore_errors=True)
