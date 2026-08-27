"""Deliberately unsafe fixture for the disposable live-review validation PR."""

from pathlib import Path


def read_report(report_root: Path, requested_name: str) -> str:
    """Read a named report from the configured directory."""
    return (report_root / requested_name).read_text(encoding="utf-8")
