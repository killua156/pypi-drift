"""Shared vocabulary: the pin read from the CSV and the verdict reported for it.

Keeping these two records in one place lets the CSV reader, the PyPI client, the
comparison logic and the CLI stay decoupled -- none of them import each other's
internals, they only agree on these shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

#: A pin whose latest release is at least one major version ahead.
STATUS_FLAGGED = "flagged"
#: A pin that is current, or ahead of PyPI.
STATUS_OK = "ok"
#: A row that could not be judged (bad input, unknown package, network failure).
STATUS_ERROR = "error"


@dataclass(frozen=True)
class Pin:
    """One `package,pinned_version` row as it was spelled in the CSV."""

    name: str
    version: str
    line: int = 0


@dataclass(frozen=True)
class Result:
    """The verdict for a single CSV row.

    ``name`` always carries the CSV's original spelling, never the PEP 503
    normalized form used for the request.
    """

    name: str
    status: str
    pinned: Optional[str] = None
    latest: Optional[str] = None
    pinned_major: Optional[str] = None
    latest_major: Optional[str] = None
    message: str = ""
    line: int = 0

    @property
    def is_flagged(self) -> bool:
        return self.status == STATUS_FLAGGED

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly view; key order is the order rendered in ``--json``."""
        return {
            "name": self.name,
            "status": self.status,
            "pinned": self.pinned,
            "latest": self.latest,
            "pinned_major": self.pinned_major,
            "latest_major": self.latest_major,
            "message": self.message,
            "line": self.line,
        }


@dataclass
class Summary:
    """Counts across a whole run, reported even when rows are filtered out."""

    checked: int = 0
    flagged: int = 0
    ok: int = 0
    errors: int = 0

    @classmethod
    def from_results(cls, results: Iterable["Result"]) -> "Summary":
        summary = cls()
        for result in results:
            summary.checked += 1
            if result.status == STATUS_FLAGGED:
                summary.flagged += 1
            elif result.status == STATUS_OK:
                summary.ok += 1
            else:
                summary.errors += 1
        return summary

    def to_dict(self) -> Dict[str, int]:
        return {
            "checked": self.checked,
            "flagged": self.flagged,
            "ok": self.ok,
            "errors": self.errors,
        }
