"""End-to-end runs against a real loopback server, with every PyPI response stubbed.

The server is always bound on port 0 -- the OS picks a free port -- so a suite
run never collides with a real ``pypi-drift serve`` or with itself. Requests are
made with ``urllib`` because the ``no_network`` fixture deliberately breaks
httpx's transports, which is exactly what should happen to the PyPI client.
"""

import json
import errno
import re
import socket
import threading
import urllib.error
import urllib.request

import httpx
import pytest

from helpers import json_response, make_client
from pypi_drift import cli, web
from pypi_drift.models import STATUS_ERROR, STATUS_FLAGGED, STATUS_OK

MIXED_TEXT = (
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

# No proxy handler: a developer's http_proxy must not swallow a loopback call.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url, method="GET", payload=None, body=None, headers=None, timeout=15):
    """Return ``(status, text, headers)`` for one request, errors included."""
    data = body
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    sent = {"Content-Type": "application/json"} if data is not None else {}
    sent.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=sent)
    try:
        with _OPENER.open(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def raw_post(url, body=b"{}", extra_headers=""):
    """Send an exact request urllib will not: no Content-Type, a bare ``null``.

    Returns the whole response text, or raises ``socket.timeout`` when the
    server never answers at all -- which is the failure being guarded against.
    """
    port = int(url.rsplit(":", 1)[1])
    message = (
        "POST /api/check HTTP/1.1\r\n"
        "Host: 127.0.0.1:{}\r\n"
        "Content-Length: {}\r\n"
        "{}"
        "Connection: close\r\n\r\n"
    ).format(port, len(body), extra_headers).encode("ascii") + body

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(message)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def check(url, text):
    """POST a pin list; return ``(status, decoded_json)``."""
    status, raw, _ = request(url + "/api/check", method="POST", payload={"text": text})
    return status, json.loads(raw)


@pytest.fixture
def serve():
    """Start servers on an OS-assigned port and shut them down afterwards."""
    started = []

    def start(**kwargs):
        server = web.create_server(port=0, **kwargs)
        # A short poll interval keeps shutdown() from costing half a second a test.
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        started.append((server, thread))
        return "http://127.0.0.1:{}".format(server.server_port)

    yield start

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


# -- the page ------------------------------------------------------------


def test_the_page_is_served_whole_with_every_control_it_needs(serve):
    status, page, _ = request(serve() + "/")
    assert status == 200
    assert "<textarea id=\"pins\"" in page
    assert "type=\"file\"" in page
    assert "id=\"check\"" in page
    assert "<tbody id=\"rows\">" in page
    assert "id=\"summary\"" in page


def test_the_page_fetches_nothing_from_anywhere(serve):
    """No build step, no CDN, no fonts: it has to work with PyPI as the only host."""
    _, page, _ = request(serve() + "/")
    external = re.findall(r"(?:src|href)\s*=\s*[\"'](?!data:)([^\"']+)", page)
    assert all(not ref.startswith(("http://", "https://", "//")) for ref in external), external
    assert "cdn" not in page.lower()


def test_the_page_never_submits_an_empty_textarea(serve):
    """The empty case is answered inline, before any request is issued."""
    _, page, _ = request(serve() + "/")
    guard = page.index("if (!text.trim())")
    assert page.index("Paste a pin list first") > guard
    assert page.index("fetch(\"/api/check\"") > guard
    assert "return;" in page[guard:page.index("fetch(\"/api/check\"")]


def test_the_page_loads_a_chosen_file_into_the_textarea_and_reports_an_unreadable_one(serve):
    """A picked file only fills the textarea; submitting it is the same one path."""
    _, page, _ = request(serve() + "/")
    handler = page[page.index("filePicker.addEventListener"):page.index("})();")]
    assert "readAsText(file)" in handler
    assert "pins.value = text;" in handler
    assert "Could not read" in handler
    assert "fetch(" not in handler, "picking a file must not be a second submit path"
    # readAsText substitutes U+FFFD instead of failing, so a cp1252 export would
    # otherwise be checked as garbled names rather than refused as the CLI does.
    assert "\\uFFFD" in handler
    assert "is not valid UTF-8 text." in handler


def test_a_second_check_cannot_start_over_an_unfinished_one(serve):
    """Two in flight would render in arrival order: a slow early answer last."""
    _, page, _ = request(serve() + "/")
    submit = page[page.index("function submit()"):page.index("button.addEventListener")]
    assert "if (button.disabled) { return; }" in submit
    assert submit.index("clearReport()") < submit.index("fetch(\"/api/check\"")


def test_the_page_locks_itself_down_with_a_policy_not_only_with_prose(serve):
    _, _, headers = request(serve() + "/")
    policy = headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "connect-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy


def test_every_response_carries_the_headers_that_keep_it_to_itself(serve, stub_pypi):
    """Deleting any of these must fail here, not merely read differently."""
    stub_pypi({"requests": "2.31.0"})
    url = serve()
    _, _, page_headers = request(url + "/")
    _, _, api_headers = request(
        url + "/api/check", method="POST", payload={"text": "requests,1.2.3"}
    )
    for headers in (page_headers, api_headers):
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Cache-Control"] == "no-store"


# -- the matrix ----------------------------------------------------------


def test_a_pasted_drifted_pin_comes_back_flagged_with_both_majors(serve, stub_pypi):
    stub_pypi({"requests": "2.31.0"})
    status, document = check(serve(), "requests,1.2.3")

    assert status == 200
    row = document["results"][0]
    assert row["status"] == STATUS_FLAGGED
    assert (row["pinned"], row["latest"]) == ("1.2.3", "2.31.0")
    assert (row["pinned_major"], row["latest_major"]) == ("1", "2")
    assert row["message"] == "1 major version behind"
    assert (document["checked"], document["flagged"], document["ok"], document["errors"]) == (1, 1, 0, 0)


def test_a_mixed_list_renders_every_row_and_the_unknown_one_does_not_blank_the_table(
    serve, stub_pypi
):
    stub_pypi(MIXED_VERSIONS)
    status, document = check(serve(), MIXED_TEXT)

    assert status == 200
    by_name = {row["name"]: row for row in document["results"]}
    assert by_name["requests"]["status"] == STATUS_FLAGGED
    assert by_name["flask"]["status"] == STATUS_OK
    assert by_name["pypi-drift-ghost"]["status"] == STATUS_ERROR
    assert by_name["pypi-drift-ghost"]["message"] == "not found on PyPI"
    assert (document["checked"], document["flagged"], document["ok"], document["errors"]) == (3, 1, 1, 1)


def test_an_empty_submission_is_refused_inline_and_asks_pypi_nothing(serve, monkeypatch):
    asked = []

    def handler(request_):
        asked.append(str(request_.url))
        return json_response("2.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    url = serve()
    for empty in ("", "   \n\n"):
        status, document = check(url, empty)
        assert status == 400
        assert "paste a pin list" in document["error"]
    assert asked == []


def test_a_list_with_no_package_rows_is_explained_not_shown_as_an_empty_table(serve):
    """The CLI prints no table for zero rows; a bare table head explains nothing."""
    url = serve()
    for text in ("package,version\n", "# just a note\n\n   \n"):
        status, document = check(url, text)
        assert status == 200, text
        assert document["checked"] == 0
        assert document["results"] == []

    _, page, _ = request(url + "/")
    assert "id=\"empty\"" in page
    assert "No package,version rows in that list" in page
    assert "scroller.hidden = results.length === 0;" in page


def test_an_unterminated_quote_is_reported_exactly_as_the_cli_reports_it(
    serve, stub_pypi, write_csv, capsys
):
    text = 'requests,2.31.0\n"broken,1.0.0\nflask,3.0.0\n'
    stub_pypi(MIXED_VERSIONS)
    _, document = check(serve(), text)

    stub_pypi(MIXED_VERSIONS)
    cli.main([write_csv(text), "--json"])
    from_cli = json.loads(capsys.readouterr().out)

    assert document == from_cli
    swallowed = [row for row in document["results"] if "unterminated quote" in row["message"]]
    assert len(swallowed) == 1
    assert swallowed[0]["message"] == (
        "line 2: unterminated quote swallowed lines 2-3; those packages were not checked"
    )
    assert swallowed[0]["status"] == STATUS_ERROR


def test_an_unreachable_pypi_still_renders_every_row_with_its_reason(serve, monkeypatch):
    def handler(request_):
        raise httpx.ConnectError("network is down", request=request_)

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    status, document = check(serve(), "requests,1.2.3\nflask,1.1.4\n")

    assert status == 200, "the page must still render"
    assert document["errors"] == 2
    assert all(row["status"] == STATUS_ERROR for row in document["results"])
    assert all(row["message"] for row in document["results"]), "every row carries its reason"


def test_a_busy_port_falls_back_to_the_next_free_one(serve):
    taken = web.create_server(port=0)
    try:
        moved = web.create_server(port=taken.server_port)
        try:
            assert moved.server_port != taken.server_port
            assert moved.server_address[0] == "127.0.0.1"
        finally:
            moved.server_close()
    finally:
        taken.server_close()


def test_an_unknown_path_is_404_and_the_server_keeps_serving(serve, stub_pypi):
    stub_pypi({"requests": "2.31.0"})
    url = serve()

    status, body, _ = request(url + "/nope")
    assert status == 404
    assert "error" in json.loads(body)

    status, body, _ = request(url + "/api/nope", method="POST", payload={"text": "requests,1.2.3"})
    assert status == 404

    assert request(url + "/")[0] == 200
    assert check(url, "requests,1.2.3")[0] == 200


def test_the_existing_cli_invocation_is_untouched_by_the_serve_route(
    write_csv, stub_pypi, capsys
):
    """`pypi-drift <csv>` must reach the same parser it always did."""
    stub_pypi(MIXED_VERSIONS)
    code = cli.main([write_csv(MIXED_TEXT)])
    out = capsys.readouterr().out
    assert code == cli.EXIT_FLAGGED
    assert "Checked 3 packages: 1 flagged, 1 ok, 1 error." in out
    assert cli.build_parser().parse_args(["pins.csv"]).csv == "pins.csv"


# -- the shared contract -------------------------------------------------


def test_the_endpoint_returns_exactly_what_json_prints_for_the_same_input(
    serve, stub_pypi, write_csv, capsys
):
    """One contract. If these two ever disagree, there are two implementations."""
    text = (
        "package,version\n"
        "requests,2.31.0\n"
        "flask,1.1.4\n"
        "six,1.16.0\n"
        "pypi-drift-ghost,1.0.0\n"
        "broken,not-a-version\n"
        "lonely\n"
        "ahead,9.0.0\n"
    )
    versions = {
        "requests": "3.0.1",
        "flask": "3.0.0",
        "six": "1.16.0",
        "pypi-drift-ghost": None,
        "ahead": "8.1.0",
    }

    stub_pypi(versions)
    status, raw_endpoint, _ = request(
        serve() + "/api/check", method="POST", payload={"text": text}
    )

    stub_pypi(versions)
    assert cli.main([write_csv(text), "--json"]) == cli.EXIT_FLAGGED
    raw_cli = capsys.readouterr().out

    assert status == 200
    assert raw_endpoint == raw_cli, "the same input must serialize to the same document"

    from_endpoint = json.loads(raw_endpoint)
    assert from_endpoint["checked"] == 7
    assert list(from_endpoint) == ["checked", "flagged", "ok", "errors", "results"]


def test_every_status_the_cli_can_produce_survives_the_round_trip(serve, stub_pypi):
    stub_pypi(MIXED_VERSIONS)
    _, document = check(serve(), MIXED_TEXT + "bad,not-a-version\nlonely\n")
    statuses = {row["status"] for row in document["results"]}
    assert statuses == {STATUS_FLAGGED, STATUS_OK, STATUS_ERROR}
    assert all(set(row) == {
        "name", "status", "pinned", "latest", "pinned_major", "latest_major", "message", "line",
    } for row in document["results"])


def test_rows_come_back_in_csv_line_order(serve, stub_pypi):
    stub_pypi({"alpha": "1.0.0", "gamma": "2.0.0", "epsilon": "3.0.0"})
    _, document = check(
        serve(),
        "alpha,1.0.0\nbeta\ngamma,2.0.0\ndelta,not-a-ver\nepsilon,3.0.0\n",
    )
    assert [row["name"] for row in document["results"]] == [
        "alpha", "beta", "gamma", "delta", "epsilon",
    ]


def test_a_bom_in_pasted_text_is_stripped_like_the_csv_reader_strips_it(serve, stub_pypi):
    """A file picked in the browser can arrive with the BOM its author saved."""
    stub_pypi({"requests": "2.31.0"})
    _, document = check(serve(), "\ufeffpackage,version\nrequests,1.2.3\n")
    assert [row["name"] for row in document["results"]] == ["requests"]
    assert document["flagged"] == 1


# -- request handling ----------------------------------------------------


def test_a_malformed_body_is_a_clean_error_and_the_server_survives(serve, stub_pypi):
    stub_pypi({"requests": "2.31.0"})
    url = serve()

    status, body, _ = request(url + "/api/check", method="POST", body=b"{not json")
    assert status == 400
    assert "JSON" in json.loads(body)["error"]

    status, body, _ = request(url + "/api/check", method="POST", payload={"text": 12})
    assert status == 400

    status, body, _ = request(url + "/api/check", method="POST", payload=["requests,1.0.0"])
    assert status == 400

    assert check(url, "requests,1.2.3")[0] == 200


@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded", "text/plain;charset=UTF-8", ""],
)
def test_a_post_that_is_not_json_is_refused(serve, content_type):
    """Exactly the shapes another origin can send with no preflight to ask about."""
    status, _, _ = request(
        serve() + "/api/check",
        method="POST",
        body=b"text=requests,1.0.0",
        headers={"Content-Type": content_type},
    )
    assert status == 415


def test_a_post_with_no_content_type_at_all_is_refused(serve):
    """urllib always sends one, so this has to be spoken raw -- a fetch need not."""
    response = raw_post(serve(), body=b'{"text": "requests,1.0.0"}')
    assert "415" in response.splitlines()[0]


def test_a_post_from_another_origin_is_refused(serve):
    status, _, _ = request(
        serve() + "/api/check",
        method="POST",
        payload={"text": "requests,1.0.0"},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403


def test_the_pages_own_origin_is_accepted(serve, stub_pypi):
    """The guard must not refuse the one origin that is supposed to be asking."""
    stub_pypi({"requests": "2.31.0"})
    url = serve()
    status, _, _ = request(
        url + "/api/check",
        method="POST",
        payload={"text": "requests,1.2.3"},
        headers={"Origin": url},
    )
    assert status == 200


def test_a_body_of_json_null_is_answered_rather_than_left_hanging(serve):
    """``null`` decodes to None, which must not read as "already answered"."""
    response = raw_post(  # socket.timeout if the server never answers at all
        serve(), body=b"null", extra_headers="Content-Type: application/json\r\n"
    )
    assert "400" in response.splitlines()[0]


def test_a_request_naming_another_host_is_refused(serve):
    """DNS rebinding: the socket is loopback, so any other Host came from elsewhere."""
    status, _, _ = request(serve() + "/", headers={"Host": "drift.example.com"})
    assert status == 403


def test_an_oversized_body_is_refused_without_reading_it(serve):
    url = serve() + "/api/check"
    status, _, _ = request(
        url,
        method="POST",
        body=b"{}",
        headers={"Content-Length": str(web.MAX_BODY_BYTES + 1)},
    )
    assert status == 413


def test_head_is_answered_here_with_the_pages_headers_and_no_body(serve):
    status, body, headers = request(serve() + "/", method="HEAD")
    assert status == 200
    assert body == ""
    assert headers["Content-Security-Policy"]
    assert int(headers["Content-Length"]) == len(web.PAGE.encode("utf-8"))


def test_another_method_is_a_405_here_not_the_base_class_501_page(serve):
    url = serve()
    status, body, headers = request(url + "/", method="OPTIONS")
    assert status == 405
    assert headers["Allow"] == web.ALLOWED_METHODS
    assert "OPTIONS" in json.loads(body)["error"]

    # and the loopback check still runs first, as it does for GET and POST
    status, _, _ = request(url + "/", method="OPTIONS", headers={"Host": "drift.example.com"})
    assert status == 403


def test_getting_the_endpoint_says_how_to_use_it(serve):
    status, body, _ = request(serve() + "/api/check")
    assert status == 405
    assert "POST" in json.loads(body)["error"]


def test_a_slow_lookup_does_not_block_the_page_from_loading(serve, monkeypatch):
    """Threaded: the tool is useless if one hung host freezes the whole server."""
    reached = threading.Event()
    release = threading.Event()

    def handler(request_):
        reached.set()
        release.wait(timeout=30)
        return json_response("2.0.0")

    monkeypatch.setattr(
        "pypi_drift.pypi.build_client", lambda *args, **kwargs: make_client(handler)
    )
    url = serve()
    slow = threading.Thread(target=lambda: check(url, "slow,1.0.0"), daemon=True)
    slow.start()
    try:
        assert reached.wait(timeout=15), "the slow lookup never reached the handler"
        # Well under the handler's wait: a serial server fails here fast rather
        # than passing whenever the GET happens to win the race.
        assert request(url + "/", timeout=5)[0] == 200
    finally:
        release.set()
        slow.join(timeout=30)


# -- the server and its command -----------------------------------------


def test_the_server_binds_loopback_and_offers_no_way_not_to():
    server = web.create_server(port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert web.HOST == "127.0.0.1"
    finally:
        server.server_close()

    help_text = cli.build_serve_parser().format_help()
    for flag in ("--host", "--bind", "--interface", "--public"):
        assert flag not in help_text
    assert "--port" in help_text and "--no-browser" in help_text


def test_serve_prints_its_url_and_returns_cleanly_on_ctrl_c(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))

    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(web.DriftServer, "serve_forever", interrupt)
    web.serve(port=0, open_browser=True)

    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:\d+/", out)
    assert "local only" in out
    assert "stopped" in out
    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:")


def test_serve_says_which_port_it_landed_on_when_the_preferred_one_is_busy(monkeypatch, capsys):
    taken = web.create_server(port=0)
    monkeypatch.setattr(web.DriftServer, "serve_forever", lambda self: None)
    try:
        code = cli.main(["serve", "--port", str(taken.server_port), "--no-browser"])
    finally:
        taken.server_close()

    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "port {} is busy".format(taken.server_port) in out
    landed = int(re.search(r"http://127\.0\.0\.1:(\d+)/", out).group(1))
    assert landed != taken.server_port


def test_no_browser_opens_no_browser(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(web.DriftServer, "serve_forever", lambda self: None)

    assert cli.main(["serve", "--no-browser"] + ["--port", "0"]) == cli.EXIT_OK
    assert opened == []


def test_a_browser_that_cannot_be_opened_is_not_a_failure(monkeypatch, capsys):
    def explode(url):
        raise RuntimeError("no display")

    monkeypatch.setattr(web.webbrowser, "open", explode)
    monkeypatch.setattr(web.DriftServer, "serve_forever", lambda self: None)
    assert cli.main(["serve", "--port", "0"]) == cli.EXIT_OK


def test_ctrl_c_before_the_server_is_serving_still_exits_clean(monkeypatch, capsys):
    """Ctrl-C during the port scan or the browser launch, not inside the loop."""

    def interrupt(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(web, "serve", interrupt)
    assert cli.main(["serve", "--no-browser"]) == cli.EXIT_OK
    assert "Traceback" not in capsys.readouterr().err


def test_a_closed_pipe_on_serve_exits_quietly(monkeypatch, capsys):
    """`pypi-drift serve | head` ends the same way the CSV path does."""

    def explode(**kwargs):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(web, "serve", explode)
    assert cli.main(["serve", "--no-browser"]) == cli.EXIT_OK
    assert capsys.readouterr().err == ""


def test_serve_rejects_a_port_outside_the_range():
    for bad in ("-1", "70000"):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["serve", "--port", bad])
        assert excinfo.value.code == cli.EXIT_ERROR


def test_serve_reports_an_unusable_port_as_an_operational_failure(monkeypatch, capsys):
    def explode(**kwargs):
        raise OSError("could not bind")

    monkeypatch.setattr(web, "create_server", explode)
    code = cli.main(["serve", "--no-browser"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_ERROR
    assert "Traceback" not in err
    assert err.count("\n") == 1


def test_a_file_named_serve_is_shadowed_by_the_subcommand_and_needs_a_path(
    tmp_path, monkeypatch, stub_pypi, capsys
):
    """The documented trade-off, asserted where it actually bites."""
    stub_pypi({"requests": "2.31.0"})
    (tmp_path / "serve").write_text("requests,1.2.3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(web.DriftServer, "serve_forever", lambda self: None)

    # The bare word starts the server; the file of that name is not read.
    assert cli.main(["serve", "--no-browser", "--port", "0"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "serving http://127.0.0.1:" in out
    assert "requests" not in out

    # Spelled as a path, it is a CSV again -- which is the documented way in.
    assert cli.main(["./serve"]) == cli.EXIT_FLAGGED
    assert "requests" in capsys.readouterr().out


def test_the_port_fallback_gives_up_rather_than_scanning_forever(monkeypatch):
    attempted = []

    def always_busy(address, handler):
        attempted.append(address[1])
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(web, "DriftServer", always_busy)
    with pytest.raises(OSError) as excinfo:
        web.create_server(port=9999, attempts=3)

    assert attempted == [9999, 10000, 10001]
    assert excinfo.value.errno == errno.EADDRINUSE, "the reason must survive"
    assert "3 ports from 9999" in str(excinfo.value)


def test_a_windows_style_bind_conflict_is_recognised_as_a_busy_port(monkeypatch):
    """Windows reports a taken port as Winsock 10048, which CPython hands back
    untranslated -- errno.EADDRINUSE never appears there, so matching only that
    would re-raise instead of moving to the next port."""
    attempted = []

    def busy_once(address, handler):
        attempted.append(address[1])
        if len(attempted) == 1:
            raise OSError(web.WSAEADDRINUSE, "Only one usage of each socket address")
        return "bound"

    monkeypatch.setattr(web, "DriftServer", busy_once)
    assert web.create_server(port=9100) == "bound"
    assert attempted == [9100, 9101], "10048 must mean busy, not fatal"


def test_the_bind_failure_reports_the_ports_that_existed_not_the_ones_asked_for(
    monkeypatch,
):
    """--port 65535 tries one port; saying it tried twenty is a lie."""

    def always_busy(address, handler):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(web, "DriftServer", always_busy)
    with pytest.raises(OSError) as excinfo:
        web.create_server(port=65535, attempts=20)
    assert "1 port from 65535" in str(excinfo.value)
