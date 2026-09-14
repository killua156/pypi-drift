---
title: 'CI workflow running the suite on every push'
type: 'chore'
created: '2026-09-14'
status: 'in-progress'
baseline_commit: 'fdd38a1492083bb2415b3c171d8e15d3a6f9883b'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nothing runs the test suite except a person remembering to. The declared floor `requires-python = ">=3.9"` is a promise nothing checks — the suite has only ever been run on 3.9 by hand, so the first 3.10-only idiom would break installs silently and no one would know until a user hit it.

**Approach:** A GitHub Actions workflow that runs the suite on every push and pull request across a matrix of Python versions, so the declared floor is enforced by machine rather than by memory.

## Boundaries & Constraints

**Always:**
- The floor named in `pyproject.toml` is one of the versions actually tested. If the two ever disagree, the workflow is wrong.
- The suite runs exactly as a developer runs it: `uv run pytest`, no special flags, no altered configuration.
- A failing job fails the run. No `continue-on-error`, no soft gates, no results that are green while something is broken.
- Runs are managed by uv, matching how the project is developed and locked.
- Superseded runs on the same ref are cancelled, so a busy branch does not queue stale jobs.
- The matrix is the declared floor and the current latest (3.9 and 3.14) on both Ubuntu and Windows: four jobs. Windows is included deliberately, to exercise the socket-reuse and `os.stat` file-type paths that differ there and have never been run.

**Never:**
- No publishing, releasing, tagging, or writing to the repository.
- No secrets, no tokens beyond the default read-only checkout credential.
- No coverage thresholds, linting gates, or any check the project does not already run locally.
- No network access assumed for the suite itself — the tests stub PyPI and must stay that way.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Healthy push | Suite passes locally | Every matrix job green | N/A |
| Broken test | A test fails on all versions | Every job red, run fails | Failure surfaced per job |
| Floor-only breakage | A 3.10+ idiom is introduced | The floor job fails while newer ones pass | The matrix names which version broke |
| Pull request | PR opened against main | Same matrix runs, result reported on the PR | N/A |
| Sandboxed runner | Runner blocks outbound network | Suite still passes; it stubs PyPI and binds only loopback | N/A |
| Rapid pushes | Second push before the first finishes | The earlier run is cancelled, not queued | N/A |

</frozen-after-approval>

## Code Map

- `pyproject.toml:6` -- `requires-python = ">=3.9"`, the promise this workflow exists to enforce. The matrix must include this version; if the floor moves, the workflow moves with it.
- `pyproject.toml` -- `[dependency-groups] dev = ["pytest>=7.0"]`, so `uv run pytest` resolves the test dependency without a separate install step.
- `tests/conftest.py:32` -- the autouse fixture that fails any real connection attempt. The suite needs no network, so a locked-down runner is fine.
- `tests/test_web.py` -- binds loopback sockets on ephemeral ports (port 0) throughout. Needs a runner that permits loopback binding, which hosted runners do; nothing binds a fixed port, so parallel jobs cannot collide.
- `.github/workflows/` -- to create. No CI configuration exists anywhere in the repo today.

## Tasks & Acceptance

**Execution:**
- [x] `.github/workflows/ci.yml` -- the workflow: triggers on push and pull request, a 3.9/3.14 x Ubuntu/Windows matrix, uv-managed setup with caching, and `uv run pytest` -- the whole deliverable
- [x] `README.md` -- note that CI runs the suite on every push and which versions it covers -- so the tested range is discoverable without opening the workflow
- [x] `_bmad-output/implementation-artifacts/deferred-work.md` -- remove the two entries this closes (no CI workflow; the unguarded 3.9 floor) -- deferred work that has been done should not keep claiming it is outstanding

**Acceptance Criteria:**
- Given a push to main, when the workflow runs, then the suite executes on every matrix version and the run is green.
- Given a change that breaks only on the declared floor, when the workflow runs, then the floor job fails and identifies the version.
- Given a runner with no outbound network, when the suite runs, then it passes.
- Given the Windows jobs, when they run, then the suite passes there too, or the failure names the platform-specific path responsible.
- Given the workflow file, when its matrix is read, then it includes the exact version named by `requires-python`.

## Implementation Notes

- `.github/workflows/ci.yml` is the whole deliverable. Triggers are bare `push:`
  and `pull_request:` (no branch filters), so every push and every PR runs it.
- `concurrency: ${{ github.workflow }}-${{ github.ref }}` with
  `cancel-in-progress: true` supersedes an in-flight run on the same ref.
- `fail-fast: false` is required by the floor-only-breakage case: with the
  default, a 3.9 failure would cancel the 3.14 jobs and the matrix could no
  longer say *which* version broke. It is not a soft gate -- a failing job still
  fails the run, and there is no `continue-on-error` anywhere.
- Setup is `astral-sh/setup-uv@v10.1.0` with `python-version` from the matrix
  (it sets `UV_PYTHON`, so uv itself provides the interpreter) and
  `enable-cache: true`; `cache-suffix` keys the cache per Python version. The
  action is pinned to an exact tag because setup-uv stopped publishing moving
  major tags after v7.
- `permissions: contents: read` is declared explicitly: the default checkout
  credential and nothing more.
- The test step is exactly `uv run pytest` -- no flags, no env, no config
  override. `[dependency-groups] dev` is installed by uv by default, so pytest
  needs no separate install step.
- `timeout-minutes: 15` caps a hung job (the suite runs in ~3s); it is a
  backstop, not a check on the code.

## Spec Change Log

## Review Triage Log

## Verification

**Commands:**
- `uv run pytest` -- expected: passes locally, matching what CI will run
- push the branch and read the Actions run -- expected: every matrix job green, and the job names state their Python version

**Results (local, 2026-09-14):**
- `uv run pytest` -- 194 passed.
- `uv run --python 3.9 pytest` -- 194 passed, so the declared floor is green
  before CI ever runs it. (This rebuilds `.venv` on 3.9; a plain `uv run pytest`
  afterwards restores the default interpreter.)
- Workflow YAML parses; matrix reads `{os: [ubuntu-latest, windows-latest],
  python-version: ["3.9", "3.14"]}` -- the versions are quoted strings, so 3.10+
  would not be truncated by YAML float parsing, and "3.9" is the exact string in
  `requires-python = ">=3.9"`.
- Not verified locally: the Windows jobs and the hosted-runner run itself. Those
  need the branch pushed and the Actions run read.
