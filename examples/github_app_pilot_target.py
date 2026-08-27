"""Disposable end-to-end review target; this file must not be merged."""

from pathlib import Path


def read_report(report_name: str) -> str:
    """Read one report from the pilot report directory."""
    return (Path("/tmp/review-agent-reports") / report_name).read_text()
