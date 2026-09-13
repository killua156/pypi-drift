---
title: 'PyPI major-version drift checker CLI'
type: 'feature'
created: '2026-09-13'
status: 'done'
baseline_commit: 'NO_VCS'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** A pinned dependency list drifts silently — nobody notices when an upstream package ships a new major version, which is where breaking changes live. Checking dozens of pins by hand does not happen.

**Approach:** A CLI that reads a CSV of `package,pinned_version`, queries PyPI's JSON API for each package's latest release, and reports which pins are behind by at least one major version.

## Boundaries & Constraints

**Always:**
- Read-only: HTTP GET on `https://pypi.org/pypi/{name}/json`, nothing else. Never install or resolve.
- "Latest release" is PyPI's own `info.version`, so pre-releases are excluded unless a project has only pre-releases.
- Major drift is decided on parsed PEP 440 `(epoch, release[0])`, never on strings: `1.4.2→2.0.0` and `1!1.0→2!1.0` flag; `1.4.2→1.9.0`, `0.5.0→0.9.0`, and a pin ahead of PyPI do not.
- Request names normalized per PEP 503; the report prints the CSV's original spelling.
- One unreachable or unknown package never aborts the run — every other row is still checked.
- Requests run concurrently under a worker cap with a per-request timeout, so a large CSV finishes fast and a hung host cannot stall the run.
- Exit codes gate CI: `0` clean, `1` when any package is flagged, `2` on operational failure (missing CSV, bad args), `3` when a package could not be checked (404, network failure, invalid pin, unreadable row) and nothing was flagged. Flagged takes precedence over unchecked, so a run with both exits `1`. `--exit-zero` forces `0` for report-only use.
- Ships as a uv-managed project (`pyproject.toml`, `requires-python >=3.9`) exposing a `pypi-drift` console script, using `httpx` for HTTP and `packaging` for PEP 440.

**Never:**
- No writes back to the CSV, no auto-bumping pins, no lockfile or requirements.txt output.
- No minor/patch updates reported as findings.
- No auth, private indexes, mirrors, caching, or persisted state.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Major drift | `requests,2.31.0`, latest `3.0.1` | Flagged row: pinned, latest, both majors | N/A |
| No drift | pinned `2.28.0` or `2.31.0`, latest `2.31.0` | OK row, not flagged | N/A |
| Pin ahead | `foo,3.0.0`, latest `2.9.0` | OK row noted "ahead of PyPI" | N/A |
| Unknown package | PyPI returns 404 | Error row, run continues | "not found on PyPI" |
| Network failure | Unreachable or past timeout | Error row after one retry, run continues | Row carries the reason |
| Bad pinned version | `foo,not-a-version` | Error row, no request issued | "invalid version" |
| Header or not | `package,version` (any case; `name`/`pinned` ok) vs `requests,2.31.0` | Header → columns by name; else positional col 0/1 | N/A |
| Blank/comment/short row | ``, `# note`, one-column row | Blanks and comments skipped silently; short row is an error row | Error row cites line number |
| Unchecked, no drift | Any error row present, nothing flagged | Full report still printed | Exits `3` |
| Missing CSV | Path does not exist | One clear message naming the path, no table, no traceback | Exits `2` |

</frozen-after-approval>

## Code Map

Greenfield: the repo holds only `_bmad/` and `_bmad-output/` — no code, no git, no tests, no conventions to match. Environment: system Python 3.9.6, `uv 0.11.22` on PATH.

- `pyproject.toml` -- to create: metadata, `httpx`+`packaging` deps, `pypi-drift` entry point, pytest config.
- `src/pypi_drift/` -- to create: `models.py` (pin record + result), `csv_input.py` (parse pin list), `pypi.py` (fetch latest, concurrency, retry), `compare.py` (PEP 440 verdict), `cli.py` (args, rendering, exit codes).
- `tests/` -- to create: per-module units with stubbed PyPI responses; no live network.
- `sample-pins.csv`, `README.md` -- to create: example input; usage, CSV format, exit codes.

## Tasks & Acceptance

**Execution:**
- [x] `pyproject.toml` -- create the uv project: `requires-python >=3.9`, `httpx`+`packaging`, `pypi-drift` script, pytest config -- fixes the shape everything else builds on
- [x] `src/pypi_drift/models.py` -- pin record and result (status flagged/ok/error, pinned, latest, majors, message) -- shared vocabulary keeps modules decoupled
- [x] `src/pypi_drift/csv_input.py` -- header detection, positional fallback, skip blanks/comments, per-line error rows -- isolates input tolerance from the network path
- [x] `src/pypi_drift/compare.py` -- PEP 440 parse and major verdict incl. epoch, pin-ahead, invalid version -- the core judgment, pure and testable
- [x] `src/pypi_drift/pypi.py` -- fetch `info.version` with PEP 503 normalization, timeout, one retry, bounded worker pool -- keeps HTTP out of the verdict logic
- [x] `src/pypi_drift/cli.py` -- args (`csv`, `--json`, `--only-flagged`, `--timeout`, `--workers`, `--exit-zero`), table and JSON rendering, summary, exit codes 0/1/2 -- the only module touching stdout and process exit
- [x] `tests/` -- cover every matrix row with stubbed responses, plus an end-to-end CLI run over a temp CSV asserting output and exit code -- the matrix is the contract
- [x] `sample-pins.csv`, `README.md` -- example input and usage/format/exit-code docs -- unusable if the CSV shape must be reverse-engineered

**Acceptance Criteria:**
- Given a CSV mixing drifted, current, and unknown packages, when the CLI runs, then every row appears with its own status and the unknown one does not abort the run.
- Given `--json`, when the CLI runs, then stdout is a single valid JSON document with the same per-row verdicts and nothing else.
- Given a 50-package CSV, when the CLI runs, then requests are issued concurrently under the worker cap with no per-package stall.
- Given `--only-flagged` and no drift, when the CLI runs, then the table is empty and the summary still states how many packages were checked.
- Given the test suite, when it runs, then it passes with no network access.

## Implementation Notes

Decisions taken while building, that the code alone does not explain:

- **Concurrency is threads, not asyncio.** `pypi.fetch_all` drives a
  `ThreadPoolExecutor` over a shared `httpx.Client` (documented thread-safe).
  This keeps the CLI synchronous and, more importantly, lets every test drive
  the real concurrency path through `httpx.MockTransport`. Pool size is
  `min(workers, len(unique_names))`, so a one-package run does not spawn 8 threads.
- **One request per distinct package.** Names are deduplicated by their PEP 503
  normalized form before dispatch, so `requests` and `Requests` in the same CSV
  cost one request -- but each CSV row still gets its own result row, printed
  with its own original spelling.
- **Invalid pins never reach the network.** `cli.check` partitions the pins on
  `parse_version` first; unparsable ones become error rows immediately, and only
  the rest are handed to `fetch_all`. This is what makes "no request issued" in
  the matrix an actual property rather than an intention.
- **Retry policy: 2 attempts total.** Retried on transport errors (including
  timeouts) and on 429/500/502/503/504. A 404 is definitive ("not found on
  PyPI") and is never retried; other 4xx are reported as-is.
- **Header detection requires both columns to be recognised.** A row is only
  treated as a header when one cell names the package column *and* another names
  the version column. Without that, a package genuinely called `name` pinned at
  `1.0` would be silently swallowed as a header.
- **Error rows exit `3`** (amended; see the Spec Change Log). Every row the
  report marks `ERROR` counts -- 404s and network failures, but equally an
  invalid pin, an invalid project name, and CSV rows that could not be read.
  They all mean the same thing: that package went unchecked. `summary.errors`
  is computed after the CSV parse errors are merged into the results, which is
  what makes a short row or an unterminated quote reach the exit code.
- **`sample-pins.csv` avoids calendar-versioned packages** for its OK rows. The
  first draft pinned `certifi` and `packaging`, both of which drift majors on a
  calendar, so the sample would have shown zero OK rows within a year. It now
  uses major-stable projects (`six`, `python-dateutil`).
- **The no-network guarantee is enforced, not assumed.** An autouse fixture in
  `tests/conftest.py` replaces `httpx.HTTPTransport.handle_request` (and the
  async equivalent) with a failing stub for every test in the suite.

## Spec Change Log

- **Trigger:** After the spec was marked `done`, the human renegotiated the frozen intent: an unchecked package must fail CI, not exit `0` alongside a clean run.
- **Amended:** The exit-code boundary gains `3` for "something could not be checked", with flagged (`1`) taking precedence when a run has both. Added an I/O matrix row pinning the new exit.
- **Known-bad state avoided:** A typo'd package name or a PyPI outage exiting `0` and reading in CI as "all pins current" — a silent pass on an incomplete report.
- **KEEP:** The existing `0`/`1`/`2` meanings and `--exit-zero` are unchanged; error rows still never abort the run, and the report content is untouched. No new flag — `--exit-zero` remains the only opt-out.

## Review Triage Log

### Iteration 3 -- amended contract: exit `3` for unchecked packages

`main` now returns `EXIT_UNCHECKED = 3` when `summary.errors` is non-zero and
nothing is flagged; flagged still wins, and `--exit-zero` still forces `0`. No
new flag. Because CSV parse errors are merged into `results` before the summary
is computed, a short row and an unterminated quote reach the exit code for free
-- verified live, not assumed.

Ten tests pin the contract, each mutation-checked: reverting to the old
"errors ignored" logic fails eight of them; inverting the flagged/unchecked
precedence fails five; narrowing `--exit-zero` so it no longer covers unchecked
rows fails two; returning `3` unconditionally fails six. One pre-existing test
(`test_invalid_pinned_version_is_an_error_row_and_issues_no_request`) had
encoded the old contract and was updated.

The README's exit-code table, its "error rows do not change the exit code"
paragraph, and its `--json`-parsing CI advice were all wrong under the new
contract. The advice is now inverted: the exit code alone gates CI, and `--json`
is for reporting *which* packages were involved. Both README snippets were run
to confirm they work as printed.

### Iteration 1 -- accepted in full (22 findings)

**Correctness (silent wrong answers, the serious ones):**
- An unterminated quote made `csv.reader` swallow every following row into one
  field, so packages vanished from the report with no error row. Cells are now
  checked for embedded newlines and the swallowed line range is reported.
- Package names went into the URL unescaped and unvalidated, so `q?a=1` became a
  query string and reported package `q`'s version -- a wrong answer presented as
  a right one. Names are now validated against the PEP 508 grammar and
  percent-escaped.
- A directory or socket passed the exists check and then failed obscurely.
  `os.stat` now gates on the file type: directories and anything that is not a
  regular file, FIFO or character device are refused with a clear message.

**Robustness:** BrokenPipeError on `| head`, KeyboardInterrupt, and unexpected
exceptions from client construction all escaped as tracebacks; `main` now
converts each into one stderr line and a defined exit code. `--timeout nan/inf`
and an unbounded `--workers` are rejected. Retries pause before the second
attempt, honoring `Retry-After` on a 429 up to a cap.

**Test quality:** the review found several fixes that would pass a mutated
build. Each new test was mutation-checked -- the fix was reverted and the test
confirmed to fail -- for row ordering, `--timeout`/`--workers` reaching the HTTP
layer, the JSON `message`/`line` fields, the worker crash guard, the retry
pause, whitespace flattening, and all three correctness fixes above. The
concurrency test now gates on a `threading.Barrier` rather than wall-clock, so
it cannot flake on a loaded runner. The FIFO test carries a `SIGALRM` so a
regression fails in 5s instead of hanging the suite.

### Iteration 2 -- one regression, fixed

The iteration-1 FIFO guard used `os.path.isfile`, which was too broad: it
rejected process substitution (`pypi-drift <(grep django pins.csv)` arrives as
a FIFO at `/dev/fd/N`) and `/dev/stdin`, both of which had worked. The guard now
rejects directories and anything that is not a regular file, FIFO or character
device, so those two work again. The hang it was meant to prevent -- a FIFO with
no writer -- is left alone, because `cat` on such a FIFO blocks too; that is
standard behavior, not a defect. The FIFO test now asserts that a FIFO *with* a
writer is read successfully, and a socket covers the still-rejected branch.

**Packaging:** version is now single-sourced from `__init__.py` via
`[tool.hatch.version]`, with a LICENSE file, a shipped `py.typed`, and a
`.gitignore`. Test helpers moved to `tests/helpers.py` with
`pythonpath = ["tests"]`, so the suite survives `--import-mode=importlib` and a
future `tests/__init__.py`.

| # | Finding (source) | Verdict | Evidence | Route |
|---|---|---|---|---|
| 1 | Unterminated quote swallows all following rows (edge) | high | Reproduced: `requests / "broken / flask / six` yields 1 pin + 1 misleading "line 4" error; flask and six vanish unreported. Silent under-reporting defeats the intent. | patch |
| 2 | Package name interpolated into URL unescaped (blind, edge) | medium | Reproduced: `q?a=1` -> `/pypi/q?a=1/json` hits `/pypi/q` and reports another package's version; `a/b` adds a path segment. | patch |
| 3 | BrokenPipeError traceback on `\| head` (blind, edge) | medium | Reproduced at cli.py:113 once output exceeds the pipe buffer. Contradicts the no-traceback guarantee the CLI tests assert elsewhere. | patch |
| 4 | 429/5xx retried immediately, no backoff or Retry-After (blind, edge) | medium | Code read: RETRYABLE_STATUS loops with no sleep, doubling load against a rate-limiting host. | patch |
| 5 | FIFO / non-regular file hangs forever (edge) | medium | Reproduced: `os.path.exists` True, `isdir` False, so `open()` blocks with no output. | patch |
| 6 | KeyboardInterrupt / unexpected exception -> traceback, wrong exit (edge) | medium | Code read: no try/except around main body; Ctrl-C or a bad SSL_CERT_FILE escapes as a traceback instead of exit 2. | patch |
| 7 | `--timeout nan/inf` accepted (edge) | low | Reproduced: passes the `<= 0` check and reaches httpx. | patch |
| 8 | `--workers` unbounded (edge) | low | Code read: pool capped only by package count; a huge CSV plus a huge value exhausts threads. | patch |
| 9 | Newline/tab in a cell breaks table layout (edge) | low | Reproduced: one row renders as two lines. | patch |
| 10 | Test gaps: row order, --timeout/--workers reaching HTTP, non-UTF-8, csv.Error, JSON message/line, worker crash guard (verification-gap x6) | medium | Pre-verified by the layer: each surface deleted in an isolated copy leaves all 96 tests green. | patch |
| 11 | Concurrency test is timing-based and flaky; three other tests over-claim (verification-gap other, blind, edge) | medium | Wall-clock and `peak > 1` assertions can fail on a loaded runner; build_client test never asserts pool size; one test duplicates the autouse fixture. | patch |
| 12 | README says `--exit-zero` always exits 0 (blind) | low | Reproduced: returns 2 on a missing CSV. argparse help is accurate; README is not. | patch |
| 13 | JSON schema undocumented though README designates it the CI gate (blind) | low | README documents no keys for the one machine-readable contract. | patch |
| 14 | Version duplicated in pyproject and `__init__` (blind) | low | Two sources of truth that will drift - pointed, in a drift checker. | patch |
| 15 | `license = MIT` declared with no LICENSE file (blind) | low | Declaration without the file. | patch |
| 16 | No .gitignore; .venv/__pycache__/.pytest_cache are loose (blind) | low | Confirmed absent; clutter the moment anyone runs `git init`. | patch |
| 17 | No py.typed despite full annotations; `Summary.from_results` unannotated (blind) | low | Type checkers treat the installed package as untyped. | patch |
| 18 | `from conftest import ...` breaks under `--import-mode=importlib` (blind) | low | Reproduced: 2 collection errors. Default mode is fine. | patch |
| 19 | `no_network` docstring over-claims (blind) | low | Patches only httpx transports, not raw sockets. | patch |
| 20 | USER_AGENT advertises a URL that does not exist (blind) | low | `https://pypi.org/project/pypi-drift/` is unpublished. | patch |
| 21 | Unreachable `except CsvInputError: raise` (verification-gap other) | low | Nothing inside the try raises it. Direct deletion. | patch |
| 22 | README documents no `pip install` path (blind) | low | Console script and wheel target are configured but undocumented. | patch |
| 23 | `requires-python >=3.9` verified by nothing (verification-gap) | medium (unverified floor) | Suite runs on 3.14 only; no CI exists. Syntax-clean on 3.9 today, so an unguarded floor, not a present break. | defer |
| 24 | No CI workflow (blind) | low | No VCS in this project yet, so there is nothing to hang CI off. | defer |
| 25 | No total deadline; slow-drip response can outlive --timeout (edge x2) | maybe-false | httpx bounds each phase, not the whole request. Would need a deliberately drip-feeding server to settle; PyPI is the only host contacted. | defer |
| 26 | Retry "contradicts per-request timeout docs" (edge) | false | README says *per-request*, and a retry is a second request. 2x total is the documented behavior, not a contradiction. | reject |
| 27 | Duplicate/conflicting pins pass silently (blind) | low | Reproduced, but reporting one row per CSV row is defensible; the fix adds branching for an unlikely CSV mistake. | reject |
| 28 | No `--index-url` / private index support (blind) | false | The frozen intent explicitly excludes private indexes and mirrors. | reject |
| 29 | No `--fail-on-error` flag (blind) | n/a | Fix would edit this build's frozen spec. Raised to the human as a decision instead. | reject |
| 30 | No stdin (`-`) input support (blind) | low | New surface with no basis in the intent. | reject |

## Verification

**Commands:**
- `uv run pytest` -- expected: all pass, no network access
- `uv run pypi-drift sample-pins.csv` -- expected: one row per package, a summary line, exit `1` (the sample drifts; flagged wins over its unknown package)
- exit codes across inputs -- expected: `0` clean, `1` any drift, `2` missing CSV or bad args, `3` unchecked package with no drift
- `uv run pypi-drift sample-pins.csv --json | python3 -m json.tool` -- expected: parses cleanly
- `uv run pypi-drift sample-pins.csv --exit-zero` -- expected: same report, exit `0`
- `uv run pypi-drift missing.csv` -- expected: clear message naming the path, exit `2`, no traceback
