---
title: 'Local web UI for pypi-drift'
type: 'feature'
created: '2026-09-13'
status: 'done'
baseline_commit: 'f59494c5ecc770854881cbd2d0c235f3151b038f'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Checking pins means having the CSV on disk and a terminal to run it in. Someone holding a pin list in a chat message or a spreadsheet cell has no way to ask the question without first making a file.

**Approach:** `pypi-drift serve` starts a small local HTTP server and opens a page where you paste a pin list (or pick a file) and get the same drift table back in the browser, computed by the same modules the CLI uses.

## Boundaries & Constraints

**Always:**
- One verdict implementation: the server reuses `csv_input.parse_rows`, `cli.check`, `Summary.from_results` and `Result.to_dict`. No second copy of the parsing, comparison, or fetch logic.
- The JSON the endpoint returns is the same document shape `--json` prints, so both surfaces carry one contract.
- Bind loopback only, with no flag offering otherwise. The server is reachable from this machine and nowhere else; a teammate runs their own copy.
- The existing CLI is untouched: `pypi-drift pins.csv` and every flag keep working exactly as now, and the four exit codes keep their meanings. `serve` is an addition, not a restructure.
- Zero new dependencies: the HTTP layer is stdlib `http.server`, keeping the project at `httpx` + `packaging`.
- The page is one self-contained document served by the tool: no build step, no package manager, no CDN, no fonts or assets fetched from anywhere. It works with no internet beyond the PyPI lookups themselves.
- Every verdict the CLI can produce renders in the browser — flagged, ok, error rows and the summary line — with nothing dropped because it is inconvenient to display.

**Never:**
- No persistence: nothing written to disk, no history, no saved pin lists.
- No auth, accounts, sessions, or multi-user behavior — this is a single-operator local tool.
- No binding to a public interface, and no flag that offers to.
- No editing or exporting pins back out; the page answers a question, it does not manage a file.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Paste with drift | `requests,1.2.3` | Table row marked flagged, with both majors and the summary line | N/A |
| Mixed list | Drifted, current and unknown packages | Every row rendered with its own status; the unknown one does not blank the table | Error row inline |
| Empty submit | Textarea empty | Inline message asking for a pin list; no request issued | N/A |
| Unterminated quote | A stray `"` in the pasted text | The swallowed range is reported exactly as the CLI reports it | Error row citing the lines |
| PyPI unreachable | Network down mid-check | Page still renders, every affected row carries its reason | Error rows |
| File picked | User chooses a `.csv` | Its text fills the textarea; submission path is identical to pasting | Unreadable file reported inline |
| Port busy | Default port already bound | Server starts on the next free port and says which | N/A |
| Unknown path | `GET /nope` | 404, server stays up | N/A |
| CLI unchanged | `pypi-drift pins.csv` | Identical output and exit code to before this change | N/A |

</frozen-after-approval>

## Code Map

The package is small and already factored for this — the verdict path takes data, not files.

- `src/pypi_drift/csv_input.py:68` -- `parse_rows(lines)` takes an iterable of lines, so pasted text needs no temp file. Returns `(pins, error_rows)`. Reuse as-is.
- `src/pypi_drift/cli.py:86` -- `check(pins, timeout, workers)` returns `List[Result]`. The whole fetch-and-compare path. Reuse as-is.
- `src/pypi_drift/models.py:31,66` -- `Result.to_dict()` and `Summary.from_results()` already produce the `--json` shape. Reuse for the response body.
- `src/pypi_drift/cli.py:38,180` -- `build_parser()` takes a **positional** `csv`, so a bare `serve` subcommand would be parsed as a file path named "serve". This is the one real integration hazard: `serve` must be routed before the existing parser sees it, and `pypi-drift pins.csv` must still parse exactly as it does today.
- `src/pypi_drift/web.py` -- to create: the server, the request handlers, and the page.
- `tests/test_web.py` -- to create. `tests/helpers.py` and the autouse `no_network` fixture in `tests/conftest.py:1` already stub PyPI; reuse them rather than adding a second stubbing mechanism.

## Tasks & Acceptance

**Execution:**
- [x] `src/pypi_drift/web.py` -- the page: one self-contained HTML document with a textarea, a file picker, a submit control, a results table and the summary line -- the whole UI, with no external assets
- [x] `src/pypi_drift/web.py` -- the endpoint: `POST /api/check` taking the pasted text, running it through `parse_rows` then `check`, returning the `--json` document shape -- one contract shared with the CLI
- [x] `src/pypi_drift/web.py` -- the server: threaded loopback HTTP server, port fallback when busy, 404 for unknown paths, optional browser launch -- so `serve` is a working command and not a demo
- [x] `src/pypi_drift/cli.py` -- route `serve` to the new command ahead of the existing positional parser, with its own flags (`--port`, `--no-browser`) -- without disturbing how `pypi-drift <csv>` parses today
- [x] `tests/test_web.py` -- cover every matrix row against the server with PyPI stubbed, plus a test asserting the endpoint's JSON matches what `--json` produces for the same input -- the shared contract is the thing most likely to drift
- [x] `README.md` -- document `serve`, its flags, and that it is loopback-only -- so nobody assumes it is shareable

**Acceptance Criteria:**
- Given a pasted list mixing drifted, current and unknown packages, when submitted, then every row appears with its own status and the summary counts them all.
- Given the same list, when checked through the CLI with `--json`, then the endpoint's response carries identical verdicts.
- Given the existing CLI test suite, when it runs after this change, then every test passes unaltered.
- Given a request to an unknown path, when it is made, then the server responds 404 and keeps serving.
- Given the test suite, when it runs, then it makes no network calls and binds no fixed port that could collide.

## Implementation Notes

- **Routing.** `main()` now reads `argv` into a list and, when the first word is
  exactly `serve`, hands the rest to `_serve()`. `build_parser()` and everything
  downstream of it is untouched, so `pypi-drift pins.csv` takes the same path it
  always did. A CSV *path* ending in `serve` (`./serve`, `/tmp/x/serve`) is still
  read as a file; only the bare first word is the subcommand.
- **One document, byte for byte.** `web.check_text()` runs the CLI's own steps in
  the CLI's order (`parse_rows` -> `check` -> extend with parse errors -> sort by
  `(line, name)` -> `Summary.from_results`) and serializes with the same
  `indent=2, sort_keys=False`. The contract test compares raw response text to
  raw `--json` stdout, not decoded objects, so a formatting drift fails too.
- **Pasted text vs. a file on disk.** `parse_rows` is fed `io.StringIO(text,
  newline="")`, matching `open(..., newline="")` in `read_pins`, so an
  unterminated quote counts the same lines either way. A leading BOM is stripped,
  which is what `encoding="utf-8-sig"` does for the CLI.
- **Imports stay lazy in both directions.** `cli` imports `web` inside
  `build_serve_parser()`/`_serve()`, and `web` imports `cli.check` inside
  `check_text()`. No cycle, and a plain CSV run never loads `http.server`.
- **Ctrl-C.** `serve()` does *not* call `server.shutdown()` in its `finally`:
  `serve_forever` has already returned, and `shutdown()` waits on an event only
  `serve_forever` sets, so calling it from that thread hangs. `server_close()` is
  the whole cleanup.
- **Hardening beyond the matrix** (a local server still listens on a port a
  browser can reach): the `Host` header must name loopback, a non-JSON
  `Content-Type` is refused 415 so a cross-origin form post is not a client, the
  body is capped at 4 MiB, and no CORS header is ever sent.
- **What the tests can and cannot reach.** The two client-side matrix rows --
  empty submit and file picked -- have no browser in the suite, so they are
  covered structurally (the guard precedes the `fetch`; the file handler only
  fills the textarea and never submits) plus a server-side mirror (an empty
  `text` is refused 400 and asks PyPI nothing). Both were additionally driven
  through the page's real JS against a stub DOM during verification.

## Spec Change Log

## Review Triage Log

| # | Finding (source) | Verdict | Evidence | Route |
|---|---|---|---|---|
| 1 | Content-Type guard bypassed by a missing/empty header; no Origin check (blind, edge) | medium | Reproduced raw: no header -> 200, empty value -> 200, `text/plain` -> 415. `media_type and` short-circuits the guard. Cross-origin `Origin` also accepted. A page in another tab can drive local PyPI lookups (cannot read them back). | patch |
| 2 | JSON `null` body -> no response at all (edge) | medium | Reproduced: connection hangs to timeout. `_read_json_body` uses `None` as its error sentinel, and `null` decodes to `None`, so the handler returns without replying. | patch |
| 3 | HEAD/OPTIONS/PUT fall through to the base class 501, skipping the loopback check and JSON error contract (blind, edge) | low | Reproduced: `HEAD /` -> 501 HTML. | patch |
| 4 | Ctrl/Cmd+Enter bypasses the in-flight guard; a failed check leaves the stale table above the error (blind, edge) | low | `submit()` sets `button.disabled` but never reads it, and the key handler does not consult it. Concurrent responses render in arrival order. | patch |
| 5 | Header-only or comment-only paste renders an empty grid plus "Checked 0 packages" (blind, edge) | low | The CLI deliberately prints nothing for zero rows; the page shows a bare table head where the user is most confused. | patch |
| 6 | `serve` can never set timeout/workers, yet both are plumbed through three call layers (blind, verification-gap) | medium | Pre-verified: deleting the assignments in `create_server` changes nothing any test can see. Dead configuration surface that reads as configurable. | patch |
| 7 | `check_text` is a second arrangement of the CLI's document assembly and serialization (blind) | medium | Both sides repeat check -> extend -> sort -> summary -> dumps, held together only by one byte-equality test that would miss a later `ensure_ascii` or `default=` on one side. | patch |
| 8 | A non-UTF-8 file picked in the browser is silently mangled where the CLI refuses it (blind) | low | `readAsText` substitutes U+FFFD without firing `onerror`, so an Excel cp1252 export becomes garbled names reported as "not found on PyPI". | patch |
| 9 | Security headers (`nosniff`, `DENY`, `no-store`) pinned by no test (verification-gap) | medium | Pre-verified: deleting all three `send_header` calls leaves the suite green; the test helper discards the response object entirely. `X-Frame-Options` is the only thing preventing framing today. | patch |
| 10 | The "slow lookup does not block the page" test is a coin flip as a detector (verification-gap) | medium | Pre-verified: with a serial `HTTPServer` it failed 4 of 8 runs and passed the other 4. No barrier ensures the slow POST is in the handler before the GET is issued. | patch |
| 11 | Ctrl-C on the `serve` path has no test on its only handler (verification-gap) | medium | Pre-verified: deleting `except KeyboardInterrupt: return EXIT_OK` from `_serve` leaves 179 tests passing, while real Ctrl-C during browser launch would print a traceback and exit 130. | patch |
| 12 | The `serve`-shadowing test uses an absolute path that was never at risk (edge) | medium | The case routing actually shadows is the bare relative argument `serve`; no test covers it. | patch |
| 13 | README silent on serve's exit codes, the 4 MB body cap, the port-scan limit, the `./serve` shadowing, and that the suite now binds sockets (blind) | low | All five verified absent from the doc. | patch |
| 14 | `allow_reuse_address = True` restates a default and, on Windows, would let a second server bind a live port (blind, edge) | low | Would defeat both the fallback and its test on that platform. Cannot be exercised here; the fix is one line and correct regardless. | patch |
| 15 | Page accessibility gaps: hidden file input behind a bare `tabindex` label, live region emptied on success, no `scope` on headers, no `noscript` (blind) | low | Screen reader hears "Checking..." then silence; with JS off the page silently does nothing. | patch |
| 16 | No BrokenPipeError handling on the serve path (edge) | low | `pypi-drift serve \| head` would produce flush noise and exit 2 where the CSV path exits 0 cleanly. | patch |
| 17 | No Content-Security-Policy despite a fully self-contained page (blind) | low | A CSP would enforce mechanically what the README asserts by prose and one substring test. | patch |
| 18 | `create_server`'s bind-failure message reports the wrong attempt count and drops the errno (blind) | low | `--port 65535` tries one port and reports twenty; the re-raise discards `last_error`. | patch |
| 19 | The page's JavaScript is verified only by source-text matching (verification-gap) | medium | Pre-verified: swapping the PINNED/LATEST cells, deleting the NOTE cell, or sending `{text: ""}` each leave all 179 tests passing. The third makes the page completely unusable. | defer |
| 20 | No cancellation, no pin-count cap, no per-tab concurrency limit (blind, edge) | low | A 4 MB paste is hours of lookups with no exit but killing the server. Self-inflicted, and the fix is a feature rather than a correction. | defer |
| 21 | `Content-Length: +10` / `1_0` desyncs request framing (edge) | maybe-false | Tested `+10`: returns 400, no desync observed. The claimed consequence needs keep-alive plus leftover bytes, which did not reproduce; on a loopback single-operator tool the impact would be low either way. | reject |
| 22 | A CSV named literally `serve` is shadowed by the subcommand (edge) | low | Reproduced, and inherent to routing a bare word ahead of a positional argument - the approach the spec's own Code Map directed. Standard subcommand behavior; documenting it is the proportionate fix (see #13). | reject |

## Verification

**Commands:**
- `uv run pytest` -- expected: all pass, no network access
  - **Run:** 179 passed in 2.4s (148 pre-existing, unaltered; 31 new in `tests/test_web.py`).
- `uv run pypi-drift sample-pins.csv` -- expected: unchanged output, exit `1`
  - **Run:** identical to the table in `README.md`, exit `1`.
- `uv run pypi-drift serve --no-browser` -- expected: prints its URL, serves the page, answers a POST, exits cleanly on Ctrl-C
  - **Run:** printed `http://127.0.0.1:8765/`, served the page, answered the
    README's `curl` against live PyPI, returned 404 for `/nope` and kept serving,
    exited `0` on SIGINT. A second instance on a busy port printed
    `port 8792 is busy, using 8793`.
