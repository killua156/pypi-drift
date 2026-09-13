# pypi-drift

Read a CSV of pinned Python dependencies, ask PyPI what each package's latest
release is, and report the pins that are **at least one major version behind** --
where the breaking changes live.

It is strictly read-only: an HTTP `GET` on `https://pypi.org/pypi/{name}/json`
and nothing else. It never installs, resolves, writes back to the CSV, or bumps
a pin for you.

## Install / run

Install the `pypi-drift` command:

```sh
pipx install .          # isolated, on your PATH
pip install .           # into the current environment
uv tool install .       # uv's equivalent of pipx
```

Or, to work in the repo without installing anything, the project is uv-managed:

```sh
uv run pypi-drift sample-pins.csv
```

Requires Python 3.9 or newer.

## CSV format

Two columns: the package name and the pinned version.

```csv
package,version
requests,2.31.0
urllib3,1.26.18
```

- **Header optional.** If the first non-blank row names its columns, the columns
  are read by name -- `package`, `name` or `package_name` for the first, and
  `version`, `pinned` or `pinned_version` for the second, in any case and in
  either order. Without a header, column 0 is the name and column 1 the version.
- **Blank lines and `#` comments are skipped** silently.
- **Extra columns are ignored.**
- Package names are normalized per [PEP 503](https://peps.python.org/pep-0503/)
  for the request; the report prints the spelling from your CSV.

## What counts as drift

The verdict is made on parsed [PEP 440](https://peps.python.org/pep-0440/)
versions -- the `(epoch, first release segment)` pair -- never on strings.

| Pinned | Latest | Verdict |
|---|---|---|
| `1.4.2` | `2.0.0` | flagged -- 1 major version behind |
| `1!1.0` | `2!1.0` | flagged -- epoch 1 -> 2 |
| `1.4.2` | `1.9.0` | ok |
| `0.5.0` | `0.9.0` | ok |
| `3.0.0` | `2.9.0` | ok, noted `ahead of PyPI` |

"Latest" is PyPI's own `info.version`, so pre-releases are excluded unless a
project has only pre-releases.

Minor and patch updates are never reported. This tool answers one question.

## Resilience

An unknown package, an unreachable host, or a pin that is not a version becomes
an **error row** -- the run continues and every other package is still checked.
Network failures are retried once, after a short pause, before the row is given
up on; a `429` honours the server's `Retry-After` up to a cap. Requests run
concurrently under `--workers`, each with its own `--timeout`, so a large CSV
finishes quickly and one hung host cannot stall the run.

Malformed input is reported rather than skipped: an unterminated quote (which
makes the CSV reader swallow every row after it) and a name that is not a valid
PEP 508 project name both produce error rows naming the line, so no package ever
disappears from the report without explanation.

## Options

```
pypi-drift CSV [--json] [--only-flagged] [--timeout SECONDS] [--workers N] [--exit-zero]
```

| Option | Meaning |
|---|---|
| `--json` | Emit a single JSON document on stdout and nothing else |
| `--only-flagged` | List only drifted packages; the summary still counts them all |
| `--timeout` | Per-request timeout in seconds (default: 10) |
| `--workers` | Maximum concurrent requests, 1-64 (default: 8) |
| `--exit-zero` | Exit `0` even when packages are flagged or unchecked, for report-only use; operational failures (exit `2`) are unaffected |

## JSON output

`--json` writes one JSON object to stdout and nothing else, so it is safe to
pipe. Top-level keys:

| Key | Type | Meaning |
|---|---|---|
| `checked` | int | Rows considered, including error rows |
| `flagged` | int | Rows at least one major behind |
| `ok` | int | Rows current or ahead |
| `errors` | int | Rows that could not be judged |
| `results` | array | One object per row, in CSV line order |

The counts always describe the whole run; `--only-flagged` filters `results`
alone. Each entry of `results` carries:

| Field | Type | Meaning |
|---|---|---|
| `name` | string | The package, spelled as in your CSV |
| `status` | string | `flagged`, `ok`, or `error` |
| `pinned` | string or null | The pinned version as written |
| `latest` | string or null | PyPI's `info.version`; null on an error row |
| `pinned_major` | string or null | Pinned major, epoch-qualified (`2`, `1!2`) |
| `latest_major` | string or null | Latest major, same form |
| `message` | string | Why it was flagged, or why the row failed |
| `line` | int | The CSV line the row came from |

`status` maps onto the exit code: any `flagged` row makes the run exit `1`; if
there are none but some row is an `error`, it exits `3`. You do not need to
parse the JSON to gate CI -- the exit code already carries that -- but it is the
way to report *which* packages were involved:

```sh
uv run pypi-drift pins.csv --json --exit-zero \
  | python3 -c 'import json,sys; [print(r["name"], r["status"], r["message"]) for r in json.load(sys.stdin)["results"] if r["status"] != "ok"]'
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Clean -- every package checked, nothing flagged |
| `1` | At least one package is a major version behind |
| `2` | Operational failure -- missing CSV, bad arguments |
| `3` | At least one package could not be checked, and nothing was flagged |

`1` takes precedence over `3`, so a run with both drift and errors exits `1`.

Code `3` covers every row the report marks `ERROR`: an unknown package, a
network failure, a pin that is not a valid version, a name that is not a valid
project name, and CSV rows that could not be read (a short row, an unterminated
quote). Those packages went unchecked, so the report is incomplete -- it must
not read in CI as "all pins current".

`--exit-zero` forces `0` regardless, for report-only use. It is the only
opt-out; operational failures (`2`) are unaffected.

So plain `pypi-drift pins.csv` is already enough to gate CI -- a non-zero exit
means drift, an incomplete check, or a broken invocation. Parse `--json` only
when you want to treat those cases differently from each other:

```sh
uv run pypi-drift pins.csv          # fails CI on drift *or* an unchecked package
uv run pypi-drift pins.csv || case $? in
  1) echo "pins have drifted" ;;
  2) echo "could not run" ;;
  3) echo "report incomplete" ;;
esac
```

## Example

```
$ uv run pypi-drift sample-pins.csv
STATUS   PACKAGE                         PINNED   LATEST       MAJOR   NOTE
FLAGGED  requests                        1.2.3    2.34.2       1 -> 2  1 major version behind
FLAGGED  urllib3                         1.26.18  2.7.0        1 -> 2  1 major version behind
FLAGGED  click                           7.1.2    8.5.0        7 -> 8  1 major version behind
FLAGGED  flask                           1.1.4    3.1.3        1 -> 3  2 major versions behind
OK       six                             1.16.0   1.17.0       1
OK       python-dateutil                 2.8.2    2.9.0.post0  2
ERROR    pypi-drift-no-such-package-xyz  1.0.0    -            -       not found on PyPI
Checked 7 packages: 4 flagged, 2 ok, 1 error.
$ echo $?
1
```

## Development

```sh
uv run pytest
```

The suite stubs every PyPI response and makes no network calls.
