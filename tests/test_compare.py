"""The verdict logic -- every comparison row of the spec's matrix."""

import pytest

from pypi_drift.compare import (
    evaluate,
    format_major,
    invalid_pin_result,
    major_of,
    parse_version,
)
from pypi_drift.models import STATUS_ERROR, STATUS_FLAGGED, STATUS_OK, Pin


def pin(version, name="demo"):
    return Pin(name=name, version=version, line=1)


@pytest.mark.parametrize(
    "pinned, latest",
    [
        ("2.31.0", "3.0.1"),   # the headline case
        ("1.4.2", "2.0.0"),
        ("0.9.9", "1.0.0"),    # 0.x -> 1.x is still a major step
        ("1.0", "3.0"),        # more than one major behind
        ("1!1.0", "2!1.0"),    # epoch drift, decided on (epoch, major)
    ],
)
def test_major_drift_is_flagged(pinned, latest):
    result = evaluate(pin(pinned), latest)
    assert result.status == STATUS_FLAGGED
    assert result.pinned == pinned
    assert result.latest == latest
    assert result.message


def test_flagged_row_reports_both_majors():
    result = evaluate(pin("2.31.0"), "3.0.1")
    assert result.pinned_major == "2"
    assert result.latest_major == "3"
    assert result.message == "1 major version behind"


def test_flagged_row_counts_multiple_majors_behind():
    assert evaluate(pin("1.0"), "4.0").message == "3 major versions behind"


def test_epoch_drift_renders_the_epoch_in_the_major():
    result = evaluate(pin("1!1.0"), "2!1.0")
    assert (result.pinned_major, result.latest_major) == ("1!1", "2!1")
    assert result.message == "epoch 1 -> 2"


@pytest.mark.parametrize(
    "pinned, latest",
    [
        ("2.28.0", "2.31.0"),  # minor behind is not drift
        ("2.31.0", "2.31.0"),  # exactly current
        ("0.5.0", "0.9.0"),    # 0.x minor churn is not a major step
        ("1.4.2", "1.9.0"),
        ("2.0.0rc1", "2.0.0"),  # pre-release of the same major
    ],
)
def test_no_drift_is_ok_and_unremarked(pinned, latest):
    result = evaluate(pin(pinned), latest)
    assert result.status == STATUS_OK
    assert result.message == ""


def test_pin_ahead_of_pypi_is_ok_and_noted():
    result = evaluate(pin("3.0.0"), "2.9.0")
    assert result.status == STATUS_OK
    assert result.message == "ahead of PyPI"
    assert (result.pinned_major, result.latest_major) == ("3", "2")


def test_bad_pinned_version_is_an_error_row():
    result = evaluate(pin("not-a-version"), "2.0.0")
    assert result.status == STATUS_ERROR
    assert "invalid version" in result.message
    assert result.pinned == "not-a-version"


def test_unparsable_latest_from_pypi_is_an_error_row():
    result = evaluate(pin("1.0.0"), "garbage")
    assert result.status == STATUS_ERROR
    assert "unparsable" in result.message


def test_invalid_pin_result_keeps_name_and_line():
    result = invalid_pin_result(Pin(name="Foo.Bar", version="x", line=7))
    assert (result.name, result.line, result.status) == ("Foo.Bar", 7, STATUS_ERROR)


def test_results_keep_the_csv_spelling_of_the_name():
    assert evaluate(pin("1.0", name="Flask-SQLAlchemy"), "2.0").name == "Flask-SQLAlchemy"


@pytest.mark.parametrize("raw", ["1.2.3", "1!2.0", "2.0.0rc1", " 1.0 "])
def test_parse_version_accepts_pep440(raw):
    assert parse_version(raw) is not None


@pytest.mark.parametrize("raw", ["not-a-version", "", "1.2.3.beta!", None])
def test_parse_version_rejects_non_pep440(raw):
    assert parse_version(raw) is None


def test_major_of_and_format_major():
    assert major_of(parse_version("1!2.3.4")) == (1, 2)
    assert major_of(parse_version("2.3.4")) == (0, 2)
    assert format_major((0, 2)) == "2"
    assert format_major((1, 2)) == "1!2"
