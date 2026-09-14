- source_spec: `_bmad-output/implementation-artifacts/spec-pypi-major-version-drift.md`
  summary: A slow-drip HTTP response could outlive --timeout, since httpx bounds each phase rather than the whole request.
  evidence: Filed maybe-false. httpx.Timeout(timeout) sets connect/read/write/pool individually and has no total deadline, so a server returning one byte at a time could reset the read timeout indefinitely. Settling it needs a deliberately drip-feeding test server; PyPI is the only host contacted, so real-world exposure is low.

- source_spec: `_bmad-output/implementation-artifacts/spec-serve-web-ui.md`
  summary: The web page's JavaScript is verified only by matching source text, never by running it.
  evidence: Pre-verified by mutation - swapping the PINNED/LATEST cells, deleting the NOTE cell, or changing the request body to `{text: ""}` each leave all 179 tests passing, and the third makes the page unusable. Closing it means putting a JS runtime into a two-dependency Python project; the smallest version is a node/jsdom test that loads the PAGE string, drives submit() against a stubbed fetch, and asserts the rendered cells, skipped when node is absent.

- source_spec: `_bmad-output/implementation-artifacts/spec-serve-web-ui.md`
  summary: A long check cannot be cancelled, and nothing caps how many pins one request may contain.
  evidence: The 4 MB body cap allows roughly 200k pins, which at 8 workers is hours of PyPI requests; the page offers no abort and the only exit is Ctrl-C on the server, which kills every other tab's check too. Self-inflicted for a single-operator local tool, and the fix (AbortController plus a cancel control, or a MAX_PINS ceiling) is a feature rather than a correction.
