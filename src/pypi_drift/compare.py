"""The core judgment: is a pinned version at least one major release behind?

Pure and side-effect free -- no I/O, no network -- so every row of the spec's
edge-case matrix can be exercised directly.

Majors are compared as the PEP 440 tuple ``(epoch, release[0])``, never as
strings. That makes ``1.4.2 -> 2.0.0`` and ``1!1.0 -> 2!1.0`` drift, while
``1.4.2 -> 1.9.0``, ``0.5.0 -> 0.9.0`` and a pin ahead of PyPI are not.
"""

from __future__ import annotations

from typing import Optional, Tuple

from packaging.version import InvalidVersion, Version

from .models import STATUS_ERROR, STATUS_FLAGGED, STATUS_OK, Pin, Result

Major = Tuple[int, int]


def parse_version(raw: str) -> Optional[Version]:
    """Parse a PEP 440 version, or return ``None`` when it is not one."""
    if raw is None:
        return None
    try:
        return Version(str(raw).strip())
    except (InvalidVersion, TypeError):
        return None


def major_of(version: Version) -> Major:
    """The comparable major identity of a version: ``(epoch, first release segment)``."""
    release = version.release
    return (version.epoch, release[0] if release else 0)


def format_major(major: Major) -> str:
    """Render a major for display, keeping a non-zero epoch visible (``1!2``)."""
    epoch, number = major
    return "{}!{}".format(epoch, number) if epoch else str(number)


def invalid_pin_result(pin: Pin) -> Result:
    """Error row for a pin that is not a PEP 440 version; no request is issued for it."""
    return Result(
        name=pin.name,
        status=STATUS_ERROR,
        pinned=pin.version,
        message="invalid version {!r}".format(pin.version),
        line=pin.line,
    )


def fetch_error_result(pin: Pin, reason: str) -> Result:
    """Error row for a pin whose latest release could not be retrieved."""
    return Result(
        name=pin.name,
        status=STATUS_ERROR,
        pinned=pin.version,
        message=reason,
        line=pin.line,
    )


def _drift_message(pinned_major: Major, latest_major: Major) -> str:
    """Human-readable reason a row was flagged."""
    if latest_major[0] != pinned_major[0]:
        return "epoch {} -> {}".format(pinned_major[0], latest_major[0])
    behind = latest_major[1] - pinned_major[1]
    return "{} major version{} behind".format(behind, "" if behind == 1 else "s")


def evaluate(pin: Pin, latest: str) -> Result:
    """Compare a pin against PyPI's reported latest release.

    Returns a flagged row when the latest major is strictly greater than the
    pinned major, an error row when either version is unparsable, and an ok row
    otherwise -- noting when the pin sits ahead of PyPI.
    """
    pinned_version = parse_version(pin.version)
    if pinned_version is None:
        return invalid_pin_result(pin)

    latest_version = parse_version(latest)
    if latest_version is None:
        return Result(
            name=pin.name,
            status=STATUS_ERROR,
            pinned=pin.version,
            latest=latest,
            message="PyPI reported an unparsable version {!r}".format(latest),
            line=pin.line,
        )

    pinned_major = major_of(pinned_version)
    latest_major = major_of(latest_version)

    if latest_major > pinned_major:
        status = STATUS_FLAGGED
        message = _drift_message(pinned_major, latest_major)
    else:
        status = STATUS_OK
        message = "ahead of PyPI" if pinned_version > latest_version else ""

    return Result(
        name=pin.name,
        status=status,
        pinned=pin.version,
        latest=latest,
        pinned_major=format_major(pinned_major),
        latest_major=format_major(latest_major),
        message=message,
        line=pin.line,
    )
