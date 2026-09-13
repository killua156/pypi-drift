- source_spec: `_bmad-output/implementation-artifacts/spec-pypi-major-version-drift.md`
  summary: Nothing ever runs the package on its declared Python 3.9 floor.
  evidence: The suite runs on uv's default 3.14 only; no CI, tox, or nox exists. Source is syntax-clean on 3.9 today, so this is an unguarded floor rather than a present break - the first 3.10+ idiom would break installs silently. Settle it with a CI matrix entry when CI is first set up.

- source_spec: `_bmad-output/implementation-artifacts/spec-pypi-major-version-drift.md`
  summary: No CI workflow runs the test suite.
  evidence: uv.lock is committed and `uv run pytest` is the documented check, but nothing executes it automatically. The project is not a git repository yet, so there is nothing to hang a workflow off; revisit after `git init`.

- source_spec: `_bmad-output/implementation-artifacts/spec-pypi-major-version-drift.md`
  summary: A slow-drip HTTP response could outlive --timeout, since httpx bounds each phase rather than the whole request.
  evidence: Filed maybe-false. httpx.Timeout(timeout) sets connect/read/write/pool individually and has no total deadline, so a server returning one byte at a time could reset the read timeout indefinitely. Settling it needs a deliberately drip-feeding test server; PyPI is the only host contacted, so real-world exposure is low.
