"""Disposable end-to-end review target; this file must not be merged."""

from pathlib import Path


_REPORT_ROOT = Path("/tmp/review-agent-reports")


def read_report(report_name: str) -> str:
    """Read one report from the pilot report directory."""
    root = _REPORT_ROOT.resolve()
    candidate = (root / report_name).resolve()
    if candidate.parent != root:
        raise ValueError("report_name must identify a file in the report directory")
    return candidate.read_text(encoding="utf-8")
