"""Local web UI: a loopback HTTP server wrapping the same verdict path as the CLI.

Nothing here decides anything. The page is a form, the endpoint parses the
pasted text with :func:`~pypi_drift.csv_input.parse_rows`, resolves it with
:func:`~pypi_drift.cli.check`, and returns the document
:func:`~pypi_drift.models.dump_document` writes for ``--json`` -- so the browser
and the terminal can never disagree about a verdict.

The server binds loopback and only loopback, keeps nothing on disk, and serves
one self-contained document: no build step, no CDN, no assets.
"""

from __future__ import annotations

import errno
import io
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, TextIO

from . import __version__
from .cli import check
from .csv_input import parse_rows
from .models import document_from_results, dump_document

#: The only interface this server ever binds. There is deliberately no flag.
HOST = "127.0.0.1"
#: First port tried; the next free one is used when it is taken.
DEFAULT_PORT = 8765
#: How many consecutive ports to try before giving up.
PORT_ATTEMPTS = 20

#: The page path. Everything else is a 404.
PAGE_PATHS = frozenset({"/", "/index.html"})
#: The one endpoint.
API_PATH = "/api/check"
#: What this server answers to. Anything else gets a 405 naming these.
ALLOWED_METHODS = "GET, HEAD, POST"

#: A pin list is text; anything this large is a mistake, not a paste.
MAX_BODY_BYTES = 4 * 1024 * 1024

#: Host header values a loopback server can legitimately be reached by.
ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Request bodies must be JSON. A form post is never a client of this.
JSON_CONTENT_TYPES = frozenset({"application/json"})

#: What the page actually needs: its own inline style and script, a data: URI
#: favicon, and calls back to itself. Everything else -- every other origin, and
#: being framed at all -- is refused by the browser, not merely by prose.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data:; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

#: Distinct from ``None``, which is what a body of the JSON literal ``null``
#: decodes to and a perfectly ordinary thing for a client to send.
_ANSWERED = object()


def check_text(text: str) -> Dict[str, Any]:
    """Run pasted CSV text through the CLI's own path and return its document.

    The steps and their order are the CLI's, and the document is assembled by
    the same :mod:`~pypi_drift.models` helper, because a second arrangement of
    them is a second implementation of the verdict.
    """
    if text.startswith("\ufeff"):  # what utf-8-sig strips when the CLI opens a file
        text = text[1:]
    # newline="" matches how csv_input.read_pins opens a file, so a quoted cell
    # spanning lines is counted exactly as it is from disk.
    pins, parse_errors = parse_rows(io.StringIO(text, newline=""))

    results = check(pins)
    results.extend(parse_errors)
    results.sort(key=lambda result: (result.line, result.name))
    return document_from_results(results)


class DriftServer(ThreadingHTTPServer):
    """Threaded so a slow PyPI lookup cannot block the page from loading."""


class DriftRequestHandler(BaseHTTPRequestHandler):
    """Two routes: the page, and the check endpoint. Everything else is a 404."""

    server_version = "pypi-drift/{}".format(__version__)
    sys_version = ""
    protocol_version = "HTTP/1.1"
    #: A client that promises a body and never sends it must not pin a thread.
    timeout = 30

    def log_message(self, *args):
        """Silence the per-request stderr chatter; this is a desktop tool.

        The base class calls this positionally, so swallowing the arguments is
        enough -- there is no signature to honor beyond that.
        """

    def __getattr__(self, name: str):
        """Answer every other HTTP method here rather than in the base class.

        Its 501 is an HTML page produced without the loopback check and without
        the JSON error shape every other answer has.
        """
        if name.startswith("do_"):
            return self._method_not_allowed
        raise AttributeError(name)

    def do_GET(self) -> None:  # the name http.server dispatches to
        if not self._request_is_local():
            return
        if self._path() in PAGE_PATHS:
            self._send_page()
        elif self._path() == API_PATH:
            self._error(405, "POST a JSON body to this endpoint.", allow="POST")
        else:
            self._not_found()

    def do_HEAD(self) -> None:  # the name http.server dispatches to
        if not self._request_is_local():
            return
        if self._path() in PAGE_PATHS:
            self._send_page(body=False)
        elif self._path() == API_PATH:
            self._error(405, "POST a JSON body to this endpoint.", allow="POST", body=False)
        else:
            self._not_found(body=False)

    def do_POST(self) -> None:  # the name http.server dispatches to
        if not self._request_is_local():
            return
        if self._path() != API_PATH:
            self._not_found()
            return

        media_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if media_type not in JSON_CONTENT_TYPES:
            # A missing or empty type is refused too: that is exactly the shape
            # another origin can send with no preflight to ask about.
            self._error(415, "send a JSON body (Content-Type: application/json).")
            return

        payload = self._read_json_body()
        if payload is _ANSWERED:
            return
        if not isinstance(payload, dict):
            self._error(400, "expected a JSON object with a \"text\" field.")
            return

        text = payload.get("text", "")
        if not isinstance(text, str):
            self._error(400, "\"text\" must be a string.")
            return
        if not text.strip():
            self._error(400, "paste a pin list to check.")
            return

        try:
            document = check_text(text)
        except Exception as exc:  # one bad request must never take the server down
            self._error(500, "{}: {}".format(exc.__class__.__name__, exc))
            return

        self._respond(200, _json_bytes(document), "application/json; charset=utf-8")

    # -- helpers ---------------------------------------------------------

    def _method_not_allowed(self) -> None:
        if not self._request_is_local():
            return
        self._error(
            405,
            "{} is not a method this server answers.".format(self.command),
            allow=ALLOWED_METHODS,
        )

    def _send_page(self, body: bool = True) -> None:
        self._respond(
            200,
            PAGE.encode("utf-8"),
            "text/html; charset=utf-8",
            body=body,
            headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY},
        )

    def _path(self) -> str:
        """The path with any query string dropped."""
        return self.path.split("?", 1)[0].split("#", 1)[0]

    def _request_is_local(self) -> bool:
        """Refuse anything that cannot have come from this machine's own page.

        The socket is bound to loopback, so a Host naming anything else reached
        us through a name someone pointed at 127.0.0.1 (DNS rebinding), and an
        Origin that is not ours is another site's tab asking on its own behalf.
        """
        host = self.headers.get("Host", "")
        if host and not _is_loopback_host(host):
            self._error(403, "pypi-drift serves 127.0.0.1 only.")
            return False

        origin = self.headers.get("Origin", "")
        if origin and origin not in self._own_origins():
            self._error(403, "that request did not come from this page.")
            return False
        return True

    def _own_origins(self) -> frozenset:
        """The origins this exact server is reachable at."""
        port = self.server.server_address[1]
        return frozenset(
            "http://{}:{}".format(name, port)
            for name in ("127.0.0.1", "localhost", "[::1]")
        )

    def _read_json_body(self) -> Any:
        """Return the decoded body, or ``_ANSWERED`` once an error was sent."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._error(411, "a Content-Length is required.")
            return _ANSWERED
        try:
            length = int(raw_length)
        except ValueError:
            self._error(400, "invalid Content-Length.")
            return _ANSWERED
        if length < 0:
            self._error(400, "invalid Content-Length.")
            return _ANSWERED
        if length > MAX_BODY_BYTES:
            self._error(413, "that pin list is too large to check.")
            return _ANSWERED

        body = self.rfile.read(length) if length else b""
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except UnicodeDecodeError:
            self._error(400, "the request body is not valid UTF-8 text.")
        except ValueError:
            self._error(400, "the request body is not valid JSON.")
        return _ANSWERED

    def _not_found(self, body: bool = True) -> None:
        self._error(404, "no such path: {}".format(self._path()), body=body)

    def _error(self, status: int, message: str, allow: str = "", body: bool = True) -> None:
        # A body we chose not to read is still on the wire; end the connection
        # rather than parse the leftovers as the next request.
        self.close_connection = True
        self._respond(
            status,
            _json_bytes({"error": message}),
            "application/json; charset=utf-8",
            body=body,
            headers={"Allow": allow} if allow else None,
        )

    def _respond(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        body: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            # Nothing here is for another origin to read or frame.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body:  # a HEAD carries the headers and nothing else
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The tab was closed mid-response; there is nobody left to tell.
            pass


def _is_loopback_host(host: str) -> bool:
    """True when a Host header names this machine."""
    hostname = host if host.endswith("]") else host.rsplit(":", 1)[0]
    return hostname.lower() in ALLOWED_HOSTNAMES


def _json_bytes(document: Any) -> bytes:
    stream = io.StringIO()
    dump_document(document, stream)
    return stream.getvalue().encode("utf-8")


def create_server(port: int = DEFAULT_PORT, attempts: int = PORT_ATTEMPTS) -> DriftServer:
    """Bind loopback on ``port``, or the next free port after it.

    ``port=0`` asks the OS for any free port -- what the tests use, so a suite
    run never collides with a real server or with itself.
    """
    last_error: Optional[OSError] = None
    wanted = 1 if port == 0 else max(1, attempts)
    tried = 0

    for offset in range(wanted):
        candidate = port + offset
        if candidate > 65535:
            break
        tried += 1
        try:
            server = DriftServer((HOST, candidate), DriftRequestHandler)
        except OSError as exc:
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            last_error = exc
            continue
        return server

    raise OSError(
        last_error.errno if last_error is not None else errno.EADDRINUSE,
        "could not bind {} port{} from {} onwards: {}".format(
            tried,
            "" if tried == 1 else "s",
            port,
            last_error.strerror if last_error is not None else "no port left to try",
        ),
    )


def serve(
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    stream: Optional[TextIO] = None,
) -> None:
    """Serve the page until interrupted. Returns cleanly on Ctrl-C.

    The check itself always runs with the default timeout and worker count;
    there is no page or flag to tune them from.
    """
    out = sys.stdout if stream is None else stream
    server = create_server(port=port)
    url = "http://{}:{}/".format(HOST, server.server_port)

    try:
        if port and server.server_port != port:
            out.write("pypi-drift: port {} is busy, using {}\n".format(port, server.server_port))
        out.write("pypi-drift: serving {} (local only; Ctrl-C to stop)\n".format(url))
        out.flush()

        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # a headless box has no browser; that is not a failure
                pass

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            out.write("\npypi-drift: stopped\n")
            out.flush()
    finally:
        # No shutdown() here: serve_forever has already returned, and shutdown()
        # waits on an event only serve_forever sets -- from this thread it hangs.
        server.server_close()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pypi-drift</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23b45309'/%3E%3Cpath d='M9 21V11h5a4 4 0 010 8h-2' stroke='%23fff' stroke-width='3' fill='none' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
:root {
  color-scheme: light dark;
  --bg: #f6f6f4;
  --panel: #ffffff;
  --ink: #1b1b1a;
  --muted: #6a6a66;
  --line: #dcdcd6;
  --accent: #8a5a00;
  --flagged-bg: #fdf0d5;
  --flagged-ink: #8a4b00;
  --ok-bg: #e3f1e4;
  --ok-ink: #1f5c2a;
  --error-bg: #fbe4e2;
  --error-ink: #8c2c20;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181a;
    --panel: #1e2124;
    --ink: #eceae5;
    --muted: #9b9a94;
    --line: #33373b;
    --accent: #e2a54a;
    --flagged-bg: #3b2c10;
    --flagged-ink: #f0c479;
    --ok-bg: #17301d;
    --ok-ink: #8fd39d;
    --error-bg: #3a1d1a;
    --error-ink: #f0a79c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 2rem 1rem 4rem;
  background: var(--bg);
  color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 62rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
.lede { margin: 0 0 1.25rem; color: var(--muted); max-width: 46rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .92em; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 1rem;
}
textarea {
  width: 100%;
  min-height: 11rem;
  resize: vertical;
  padding: .7rem .8rem;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: var(--bg);
  color: var(--ink);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13.5px;
}
textarea:focus-visible, button:focus-visible, #file:focus-visible + .file { outline: 2px solid var(--accent); outline-offset: 2px; }
.controls { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; margin-top: .75rem; }
button {
  font: inherit;
  font-weight: 600;
  padding: .45rem 1rem;
  border: 1px solid transparent;
  border-radius: 7px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
@media (prefers-color-scheme: dark) { button { color: #201703; } }
button[disabled] { opacity: .6; cursor: progress; }
.file {
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: .4rem .8rem;
  cursor: pointer;
  color: var(--muted);
}
/* Kept in the tab order and named by its label, unlike display:none. */
.offscreen { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; border: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.hint { color: var(--muted); font-size: .85rem; }
#message { font-size: .9rem; }
#message.warn { color: var(--flagged-ink); }
#message.bad { color: var(--error-ink); }
#message.info { color: var(--muted); }
#report { margin-top: 1.5rem; }
.scroller { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: .5rem .75rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: .74rem; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
tbody tr:last-child td { border-bottom: 0; }
td.mono, td.name { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }
td.note { color: var(--muted); }
.badge {
  display: inline-block;
  font-size: .72rem;
  font-weight: 700;
  letter-spacing: .05em;
  padding: .15rem .45rem;
  border-radius: 5px;
}
.badge.flagged { background: var(--flagged-bg); color: var(--flagged-ink); }
.badge.ok { background: var(--ok-bg); color: var(--ok-ink); }
.badge.error { background: var(--error-bg); color: var(--error-ink); }
tr.flagged td.name { font-weight: 600; }
#summary { margin: .85rem 0 0; font-variant-numeric: tabular-nums; }
footer { margin-top: 2rem; color: var(--muted); font-size: .82rem; }
</style>
</head>
<body>
<main>
  <h1>pypi-drift</h1>
  <p class="lede">Paste a list of pins &mdash; one <code>package,version</code> per line &mdash;
  and every package at least one major version behind PyPI is flagged. Same check as
  <code>pypi-drift pins.csv</code>, same verdicts.</p>

  <div class="panel">
    <label for="pins" class="hint">Pin list (a header row, <code>#</code> comments and blank lines are fine)</label>
    <textarea id="pins" spellcheck="false" autocapitalize="off" autocorrect="off"
      placeholder="package,version&#10;requests,1.2.3&#10;flask,3.0.0"></textarea>
    <div class="controls">
      <button id="check" type="button">Check pins</button>
      <input type="file" id="file" class="offscreen" accept=".csv,.txt,text/csv,text/plain">
      <label class="file" for="file">Choose a CSV&hellip;</label>
      <span id="message" role="status" aria-live="polite"></span>
    </div>
    <noscript><p class="hint">This page needs JavaScript. Without it, run
    <code>pypi-drift pins.csv</code> in a terminal instead &mdash; same check, same verdicts.</p></noscript>
  </div>

  <section id="report" hidden>
    <div class="scroller" id="scroller">
      <table>
        <thead>
          <tr><th scope="col">Status</th><th scope="col">Package</th><th scope="col">Pinned</th>
          <th scope="col">Latest</th><th scope="col">Major</th><th scope="col">Note</th></tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <p id="empty" class="hint" hidden></p>
    <p id="summary" role="status" aria-live="polite"></p>
  </section>

  <footer>Running on your machine only, on loopback. Nothing is saved; nothing leaves this
  computer except the version lookups to pypi.org.</footer>
</main>
<script>
(function () {
  var pins = document.getElementById("pins");
  var button = document.getElementById("check");
  var filePicker = document.getElementById("file");
  var message = document.getElementById("message");
  var report = document.getElementById("report");
  var scroller = document.getElementById("scroller");
  var rows = document.getElementById("rows");
  var empty = document.getElementById("empty");
  var summaryLine = document.getElementById("summary");

  function say(text, kind) {
    message.textContent = text;
    message.className = kind || "";
  }

  function plural(count, word) {
    return count === 1 ? word : word + "s";
  }

  function cell(row, text, className) {
    var td = document.createElement("td");
    td.textContent = text;
    if (className) { td.className = className; }
    row.appendChild(td);
    return td;
  }

  function majorCell(result) {
    if (result.status === "flagged") {
      return (result.pinned_major || "-") + " \\u2192 " + (result.latest_major || "-");
    }
    if (result.status === "error") { return "-"; }
    return result.pinned_major || "-";
  }

  function clearReport() {
    report.hidden = true;
    rows.textContent = "";
    summaryLine.textContent = "";
    empty.hidden = true;
  }

  function render(document_) {
    var results = document_.results || [];
    // Unhidden before the summary is written, so the live region announces it.
    report.hidden = false;
    rows.textContent = "";
    // The CLI prints no table at all for zero rows; say why instead of showing
    // a bare table head over "Checked 0 packages".
    scroller.hidden = results.length === 0;
    empty.hidden = results.length > 0;
    if (!results.length) {
      empty.textContent =
        "No package,version rows in that list \\u2014 only a header, comments or blank lines.";
    }
    results.forEach(function (result) {
      var tr = document.createElement("tr");
      tr.className = result.status;
      var status = document.createElement("td");
      var badge = document.createElement("span");
      badge.className = "badge " + result.status;
      badge.textContent = String(result.status || "").toUpperCase();
      status.appendChild(badge);
      tr.appendChild(status);
      cell(tr, result.name || "(unnamed)", "name");
      cell(tr, result.pinned || "-", "mono");
      cell(tr, result.latest || "-", "mono");
      cell(tr, majorCell(result), "mono");
      cell(tr, result.message || "", "note");
      rows.appendChild(tr);
    });
    summaryLine.textContent =
      "Checked " + document_.checked + " " + plural(document_.checked, "package") + ": " +
      document_.flagged + " flagged, " + document_.ok + " ok, " +
      document_.errors + " " + plural(document_.errors, "error") + ".";
  }

  function submit() {
    if (button.disabled) { return; }   // one check at a time, so no stale render
    var text = pins.value;
    if (!text.trim()) {
      say("Paste a pin list first \\u2014 one package,version per line.", "warn");
      pins.focus();
      return;                      // deliberately no request for an empty list
    }
    clearReport();                 // never leave the last answer under a new one
    say("Checking\\u2026", "info");
    button.disabled = true;
    fetch("/api/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok) { throw new Error(body.error || ("HTTP " + response.status)); }
        return body;
      }, function () { throw new Error("HTTP " + response.status); });
    }).then(function (body) {
      render(body);
      say("", "");
    }).catch(function (error) {
      say(error.message || "The check failed.", "bad");
    }).then(function () {
      button.disabled = false;
    });
  }

  button.addEventListener("click", submit);
  pins.addEventListener("keydown", function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { submit(); }
  });

  filePicker.addEventListener("change", function () {
    var file = filePicker.files && filePicker.files[0];
    if (!file) { return; }
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result == null ? "" : reader.result);
      filePicker.value = "";
      // readAsText never fails on bad bytes, it substitutes U+FFFD -- which
      // would otherwise be checked as garbled package names. Refuse it, the
      // way the CLI refuses the same file.
      if (text.indexOf("\\uFFFD") !== -1) {
        say(file.name + " is not valid UTF-8 text.", "bad");
        return;
      }
      pins.value = text;
      say("Loaded " + file.name + ". Check it, or edit it first.", "info");
    };
    reader.onerror = function () {
      say("Could not read " + file.name + ".", "bad");
      filePicker.value = "";
    };
    reader.readAsText(file);
  });

})();
</script>
</body>
</html>
"""
