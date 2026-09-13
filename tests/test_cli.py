"""End-to-end CLI runs over temp CSVs, with every PyPI response stubbed."""

import contextlib
import io
import json
import threading
from pathlib import Path

import httpx
import pytest

from helpers import json_response, make_client
from pypi_drift import cli
from pypi_drift.models import STATUS_ERROR, STATUS_FLAGGED, STATUS_OK

MIXED_CSV = (
    "package,version\n"
    "# a mix of drifted, current and unknown\n"
    "requests,2.31.0\n"
    "\n"
    "flask,3.0.0\n"
    "pypi-drift-ghost,1.0.0\n"
)

MIXED_VERSIONS = {
    "requests": "3.0.1",
    "flask": "3.0.0",
    "pypi-drift-ghost": None,
}


def run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_mixed_csv_reports_every_row_and_the_unknown_one_does_not_abort(
    write_csv, stub_pypi, capsys, no_network
):
    stub_pypi(MIXED_VERSIONS)
    code, out, err = run([write_csv(MIXED_CSV)], capsys)

    assert code == cli.EXIT_FLAGGED
    assert err == ""
    assert "FLAGGED" in out and "requests" in out
    assert "OK" in out and "flask" in out
    assert "ERROR" in out and "not found on PyPI" in out
    assert "Checked 3 packages: 1 flagged, 1 ok, 1 error." in out
    assert "Traceback" not in out


def test_table_rows_follow_csv_order(write_csv, stub_pypi, capsys):
    stub_pypi(MIXED_VERSIONS)
    _, out, _ = run([write_csv(MIXED_CSV)], capsys)
    body = [line for line in out.splitlines() if line and not line.startswith(("STATUS", "Checked"))]
    assert [line.split()[1] for line in body] == ["requests", "flask", "pypi-drift-ghost"]


def test_clean_run_exits_zero(write_csv, stub_pypi, capsys):
    stub_pypi({"flask": "3.0.0"})
    code, out, _ = run([write_csv("flask,3.0.0\n")], capsys)
    assert code == cli.EXIT_OK
    assert "Checked 1 package: 0 flagged, 1 ok, 0 errors." in out


def test_json_output_is_a_single_valid_document_and_nothing_else(
    write_csv, stub_pypi, capsys
):
    stub_pypi(MIXED_VERSIONS)
    code, out, err = run([write_csv(MIXED_CSV), "--json"], capsys)

    document = json.loads(out)  # would raise if anything else were printed
    assert code == cli.EXIT_FLAGGED
    assert err == ""
    assert document["checked"] == 3
    assert (document["flagged"], document["ok"], document["errors"]) == (1, 1, 1)

    by_name = {row["name"]: row for row in document["results"]}
    assert by_name["requests"]["status"] == STATUS_FLAGGED
    assert by_name["requests"]["pinned"] == "2.31.0"
    assert by_name["requests"]["latest"] == "3.0.1"
    assert by_name["requests"]["pinned_major"] == "2"
    assert by_name["requests"]["latest_major"] == "3"
    assert by_name["flask"]["status"] == STATUS_OK
    assert by_name["pypi-drift-ghost"]["status"] == STATUS_ERROR


def test_json_and_table_agree_on_every_verdict(write_csv, stub_pypi, capsys):
    stub_pypi(MIXED_VERSIONS)
    path = write_csv(MIXED_CSV)
    _, table, _ = run([path], capsys)
    _, document, _ = run([path, "--json"], capsys)
    for row in json.loads(document)["results"]:
        assert row["name"] in table
        assert row["status"].upper() in table


def test_only_flagged_hides_the_rest_but_still_counts_them(
    write_csv, stub_pypi, capsys
):
    stub_pypi(MIXED_VERSIONS)
    code, out, _ = run([write_csv(MIXED_CSV), "--only-flagged"], capsys)
    assert code == cli.EXIT_FLAGGED
    assert "requests" in out
    assert "flask" not in out
    assert "pypi-drift-ghost" not in out
    assert "Checked 3 packages: 1 flagged, 1 ok, 1 error." in out


def test_only_flagged_with_no_drift_prints_an_empty_table_and_a_summary(
    write_csv, stub_pypi, capsys
):
    stub_pypi({"flask": "3.0.0", "requests": "2.31.0"})
    code, out, _ = run(
        [write_csv("flask,3.0.0\nrequests,2.31.0\n"), "--only-flagged"], capsys
    )
    assert code == cli.EXIT_OK
    assert out == "Checked 2 packages: 0 flagged, 2 ok, 0 errors.\n"


def test_only_flagged_json_filters_rows_but_not_counts(write_csv, stub_pypi, capsys):
    stub_pypi(MIXED_VERSIONS)
    _, out, _ = run([write_csv(MIXED_CSV), "--json", "--only-flagged"], capsys)
    document = json.loads(out)
    assert document["checked"] == 3
    assert [row["name"] for row in document["results"]] == ["requests"]


def test_exit_zero_forces_a_clean_exit_without_changing_the_report(
    write_csv, stub_pypi, capsys
):
    stub_pypi(MIXED_VERSIONS)
    path = write_csv(MIXED_CSV)
    flagged_code, plain_out, _ = run([path], capsys)
    zero_code, zero_out, _ = run([path, "--exit-zero"], capsys)
    assert flagged_code == cli.EXIT_FLAGGED
    assert zero_code == cli.EXIT_OK
    assert zero_out == plain_out


def test_missing_csv_is_one_clear_message_and_exit_two(tmp_path, capsys):
    missing = str(tmp_path / "missing.csv")
    code, out, err = run([missing], capsys)
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert missing in err
    assert "Traceback" not in err
    assert err.count("\n") == 1


def test_bad_arguments_exit_two(capsys):
    for argv in ([], ["pins.csv", "--timeout", "0"], ["pins.csv", "--workers", "0"]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == cli.EXIT_ERROR


def test_invalid_pinned_version_is_an_error_row_and_issues_no_request(
    write_csv, monkeypatch, capsys
):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return json_response("2.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    code, out, _ = run([write_csv("foo,not-a-version\nbar,2.0.0\n")], capsys)

    assert code == cli.EXIT_UNCHECKED, "an unchecked pin must not read as clean"
    assert "invalid version" in out
    assert requested == ["https://pypi.org/pypi/bar/json"]


def test_short_row_is_reported_with_its_line_number(write_csv, stub_pypi, capsys):
    stub_pypi({"requests": "2.31.0"})
    code, out, _ = run([write_csv("requests,2.31.0\nlonely\n")], capsys)
    assert "line 2" in out
    assert "Checked 2 packages: 0 flagged, 1 ok, 1 error." in out


def test_network_failure_becomes_an_error_row_and_the_run_continues(
    write_csv, monkeypatch, capsys
):
    def handler(request):
        if "flaky" in str(request.url):
            raise httpx.ConnectTimeout("too slow", request=request)
        return json_response("2.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    code, out, _ = run([write_csv("flaky,1.0.0\nsteady,2.0.0\n")], capsys)
    assert "request timed out" in out
    assert "Checked 2 packages: 0 flagged, 1 ok, 1 error." in out


def test_pin_ahead_of_pypi_is_reported_as_ok(write_csv, stub_pypi, capsys):
    stub_pypi({"foo": "2.9.0"})
    code, out, _ = run([write_csv("foo,3.0.0\n")], capsys)
    assert code == cli.EXIT_OK
    assert "ahead of PyPI" in out


def test_report_prints_the_csv_spelling_not_the_normalized_name(
    write_csv, stub_pypi, capsys
):
    stub_pypi({"flask-sqlalchemy": "3.1.1"})
    _, out, _ = run([write_csv("Flask_SQLAlchemy,2.5.1\n")], capsys)
    assert "Flask_SQLAlchemy" in out
    assert "flask-sqlalchemy" not in out


def test_epoch_drift_is_flagged_end_to_end(write_csv, stub_pypi, capsys):
    stub_pypi({"foo": "2!1.0"})
    code, out, _ = run([write_csv("foo,1!1.0\n")], capsys)
    assert code == cli.EXIT_FLAGGED
    assert "1!1 -> 2!1" in out


def test_a_fifty_package_csv_is_checked_concurrently(write_csv, stub_pypi, capsys):
    versions = {"pkg-{}".format(index): "2.0.0" for index in range(50)}
    stub_pypi(versions)
    rows = "".join("pkg-{},1.0.0\n".format(index) for index in range(50))
    code, out, _ = run([write_csv("package,version\n" + rows), "--workers", "10"], capsys)
    assert code == cli.EXIT_FLAGGED
    assert "Checked 50 packages: 50 flagged, 0 ok, 0 errors." in out


def test_the_shipped_sample_csv_parses():
    """The example input must actually be readable by the tool that ships with it."""
    from pypi_drift.csv_input import read_pins

    sample = Path(__file__).resolve().parent.parent / "sample-pins.csv"
    pins, errors = read_pins(str(sample))
    assert errors == []
    assert len(pins) >= 5
    assert all(pin.name and pin.version for pin in pins)


def test_rows_are_ordered_by_csv_line_not_by_kind(write_csv, stub_pypi, capsys):
    """Error rows are produced on a different path from pins; order must still hold.

    The CSV deliberately interleaves a valid pin, a short row and an invalid
    pin, so a missing sort in main() groups them by kind and this fails.
    """
    stub_pypi({"alpha": "1.0.0", "gamma": "2.0.0", "epsilon": "3.0.0"})
    path = write_csv(
        "alpha,1.0.0\n"      # line 1: ok
        "beta\n"             # line 2: short row -> error
        "gamma,2.0.0\n"      # line 3: ok
        "delta,not-a-ver\n"  # line 4: invalid version -> error
        "epsilon,3.0.0\n"    # line 5: ok
    )
    _, out, _ = run([path], capsys)
    body = [
        line for line in out.splitlines()
        if line and not line.startswith(("STATUS", "Checked"))
    ]
    assert [line.split()[1] for line in body] == [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
    ]


def test_timeout_and_workers_reach_the_http_layer(write_csv, monkeypatch, capsys):
    """Both flags must be wired through, not merely accepted and dropped."""
    barrier = threading.Barrier(3, timeout=30)
    lock = threading.Lock()
    state = {"live": 0, "peak": 0, "timeouts": set()}

    def handler(request):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
            state["timeouts"].add(request.extensions["timeout"]["connect"])
        barrier.wait()  # deadlocks unless 3 requests really are concurrent
        with lock:
            state["live"] -= 1
        return json_response("1.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    rows = "".join("pkg-{},1.0.0\n".format(index) for index in range(9))
    code, _, _ = run([write_csv(rows), "--workers", "3", "--timeout", "4.5"], capsys)

    assert code == cli.EXIT_OK
    assert state["peak"] == 3, "--workers must cap in-flight requests"
    assert state["timeouts"] == {4.5}, "--timeout must reach the request"


def test_json_error_rows_carry_their_reason_and_csv_line(write_csv, stub_pypi, capsys):
    """--json is the CI gating surface; these are the only fields explaining a failure."""
    stub_pypi(MIXED_VERSIONS)
    _, out, _ = run([write_csv(MIXED_CSV), "--json"], capsys)
    rows = {row["name"]: row for row in json.loads(out)["results"]}

    ghost = rows["pypi-drift-ghost"]
    assert ghost["status"] == STATUS_ERROR
    assert ghost["message"] == "not found on PyPI"
    assert ghost["line"] == 6
    assert rows["requests"]["line"] == 3
    assert rows["requests"]["message"] == "1 major version behind"


def test_a_closed_pipe_exits_quietly_without_a_traceback(write_csv, stub_pypi, capsys):
    stub_pypi(MIXED_VERSIONS)

    class ClosedPipe:
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

        def fileno(self):
            return 1

    with contextlib.redirect_stdout(ClosedPipe()):
        code = cli.main([write_csv(MIXED_CSV)])
    assert code == cli.EXIT_OK


def test_ctrl_c_during_a_run_is_one_line_and_exit_two(write_csv, monkeypatch, capsys):
    monkeypatch.setattr(
        "pypi_drift.cli.check", lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    code, out, err = run([write_csv("requests,1.0.0\n")], capsys)
    assert code == cli.EXIT_ERROR
    assert err == "pypi-drift: interrupted\n"
    assert "Traceback" not in err


def test_an_unexpected_failure_is_one_line_and_exit_two(write_csv, monkeypatch, capsys):
    """A bad proxy or SSL_CERT_FILE makes client construction blow up."""

    def explode(*args, **kwargs):
        raise RuntimeError("could not load SSL certificates")

    monkeypatch.setattr("pypi_drift.pypi.build_client", explode)
    code, out, err = run([write_csv("requests,1.0.0\n")], capsys)
    assert code == cli.EXIT_ERROR
    assert err == "pypi-drift: RuntimeError: could not load SSL certificates\n"
    assert "Traceback" not in err
    assert out == ""


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-1"])
def test_non_finite_or_non_positive_timeouts_are_rejected(bad):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pins.csv", "--timeout", bad])
    assert excinfo.value.code == cli.EXIT_ERROR


@pytest.mark.parametrize("bad", ["0", "-1", "65", "100000"])
def test_workers_outside_the_supported_range_are_rejected(bad):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["pins.csv", "--workers", bad])
    assert excinfo.value.code == cli.EXIT_ERROR


def test_the_worker_cap_boundary_is_accepted(write_csv, stub_pypi, capsys):
    stub_pypi({"requests": "1.0.0"})
    code, _, _ = run([write_csv("requests,1.0.0\n"), "--workers", str(cli.MAX_WORKERS)], capsys)
    assert code == cli.EXIT_OK


def test_a_tab_inside_a_cell_does_not_break_the_table(write_csv, stub_pypi, capsys):
    """A tab renders at an unpredictable width, so the aligned columns come apart."""
    stub_pypi({"good": "1.0.0"})
    _, out, _ = run([write_csv('"we\tird",1.0.0\ngood,1.0.0\n')], capsys)
    body = [line for line in out.splitlines() if line.startswith(("OK", "ERROR", "FLAGGED"))]
    assert len(body) == 2, "one line per result"
    assert all("\t" not in line for line in body), "tabs must be collapsed before padding"


def test_a_tab_inside_a_version_does_not_break_the_table(write_csv, stub_pypi, capsys):
    """The version reaches the table twice: its own column and the error message."""
    stub_pypi({"good": "1.0.0"})
    _, out, _ = run([write_csv('good,"1.0\t0"\n')], capsys)
    body = [line for line in out.splitlines() if line.startswith(("OK", "ERROR", "FLAGGED"))]
    assert len(body) == 1
    assert "\t" not in body[0]


def test_a_newline_in_a_result_never_becomes_a_second_table_line():
    """Defence in depth: whatever the source, one Result is always one line."""
    import io

    from pypi_drift.models import STATUS_ERROR as ERR
    from pypi_drift.models import Result

    stream = io.StringIO()
    cli.render_table(
        [Result(name="a\nb", status=ERR, pinned="1\n2", message="bad\nnews", line=1)],
        stream,
    )
    assert len(stream.getvalue().splitlines()) == 2, "header plus exactly one row"


def test_a_bad_package_name_is_an_error_row_not_a_wrong_answer(
    write_csv, monkeypatch, capsys
):
    """`q?a=1` must not silently report the version of package `q`."""
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return json_response("9.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    code, out, _ = run([write_csv("q?a=1,1.0.0\ngood,1.0.0\n")], capsys)

    assert "invalid package name" in out
    assert requested == ["https://pypi.org/pypi/good/json"]
    assert "9.0.0" not in out.split("invalid package name")[0]


def test_a_non_utf8_csv_is_one_stderr_line_and_exit_two(tmp_path, capsys):
    path = tmp_path / "latin1.csv"
    path.write_bytes(b"package,version\ncaf\xe9,1.0.0\n")
    code, out, err = run([str(path)], capsys)
    assert code == cli.EXIT_ERROR
    assert out == ""
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert str(path) in err


def test_a_broken_pipe_on_a_stdout_without_a_real_fd_still_exits_cleanly(
    write_csv, stub_pypi
):
    """A substituted stdout has no file descriptor to redirect; that must not raise."""
    stub_pypi(MIXED_VERSIONS)

    class FdlessPipe:
        def write(self, _text):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            raise BrokenPipeError(32, "Broken pipe")

        def fileno(self):
            raise io.UnsupportedOperation("not a real stream")

    with contextlib.redirect_stdout(FdlessPipe()):
        code = cli.main([write_csv(MIXED_CSV)])
    assert code == cli.EXIT_OK


# --- Exit codes: 0 clean, 1 flagged, 2 operational, 3 something unchecked. ---


def test_errors_only_exits_three_not_zero(write_csv, stub_pypi, capsys):
    """The whole point: an incomplete report must never read in CI as clean."""
    stub_pypi({"good": "1.0.0", "ghost": None})
    code, out, _ = run([write_csv("good,1.0.0\nghost,1.0.0\n")], capsys)
    assert code == cli.EXIT_UNCHECKED
    assert "Checked 2 packages: 0 flagged, 1 ok, 1 error." in out


def test_flagged_takes_precedence_over_unchecked(write_csv, stub_pypi, capsys):
    """A run with both drift and errors reports the drift: exit 1, not 3."""
    stub_pypi({"drifted": "2.0.0", "ghost": None})
    code, out, _ = run([write_csv("drifted,1.0.0\nghost,1.0.0\n")], capsys)
    assert code == cli.EXIT_FLAGGED
    assert "Checked 2 packages: 1 flagged, 0 ok, 1 error." in out


def test_a_wholly_clean_run_still_exits_zero(write_csv, stub_pypi, capsys):
    stub_pypi({"a": "1.0.0", "b": "2.5.0"})
    code, out, _ = run([write_csv("a,1.0.0\nb,2.4.0\n")], capsys)
    assert code == cli.EXIT_OK
    assert "Checked 2 packages: 0 flagged, 2 ok, 0 errors." in out


def test_exit_zero_still_forces_zero_when_rows_are_unchecked(
    write_csv, stub_pypi, capsys
):
    """--exit-zero remains the only opt-out; it covers 3 as well as 1."""
    stub_pypi({"ghost": None})
    path = write_csv("ghost,1.0.0\n")
    assert run([path], capsys)[0] == cli.EXIT_UNCHECKED
    code, out, _ = run([path, "--exit-zero"], capsys)
    assert code == cli.EXIT_OK
    assert "1 error" in out, "the report is unchanged; only the exit code is forced"


def test_a_short_row_alone_exits_three(write_csv, stub_pypi, capsys):
    """A CSV parse failure leaves a package unchecked just as a 404 does."""
    stub_pypi({"good": "1.0.0"})
    code, out, _ = run([write_csv("good,1.0.0\nlonely\n")], capsys)
    assert code == cli.EXIT_UNCHECKED
    assert "line 2" in out


def test_an_unterminated_quote_alone_exits_three(write_csv, stub_pypi, capsys):
    """The swallowed packages went unchecked, so the run is not clean."""
    stub_pypi({"good": "1.0.0"})
    code, out, _ = run(
        [write_csv('good,1.0.0\n"broken,1.0.0\nflask,1.1.4\n')], capsys
    )
    assert code == cli.EXIT_UNCHECKED
    assert "unterminated quote" in out


def test_an_invalid_package_name_alone_exits_three(write_csv, stub_pypi, capsys):
    stub_pypi({"good": "1.0.0"})
    code, _, _ = run([write_csv("q?a=1,1.0.0\ngood,1.0.0\n")], capsys)
    assert code == cli.EXIT_UNCHECKED


def test_a_network_failure_alone_exits_three(write_csv, monkeypatch, capsys):
    def handler(request):
        if "flaky" in str(request.url):
            raise httpx.ConnectTimeout("too slow", request=request)
        return json_response("2.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    monkeypatch.setattr("pypi_drift.pypi.RETRY_BACKOFF", 0.0)
    code, out, _ = run([write_csv("flaky,1.0.0\nsteady,2.0.0\n")], capsys)
    assert code == cli.EXIT_UNCHECKED
    assert "request timed out" in out


def test_only_flagged_does_not_hide_an_unchecked_row_from_the_exit_code(
    write_csv, stub_pypi, capsys
):
    """Filtering the table must not filter the verdict."""
    stub_pypi({"ghost": None})
    code, out, _ = run([write_csv("ghost,1.0.0\n"), "--only-flagged"], capsys)
    assert code == cli.EXIT_UNCHECKED
    assert "ghost" not in out, "the row is hidden"
    assert "1 error" in out, "but still counted"


def test_the_four_exit_codes_are_distinct():
    codes = {cli.EXIT_OK, cli.EXIT_FLAGGED, cli.EXIT_ERROR, cli.EXIT_UNCHECKED}
    assert codes == {0, 1, 2, 3}
