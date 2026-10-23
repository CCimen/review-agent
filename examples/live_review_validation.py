"""Deliberately unsafe fixture for the disposable live-review validation PR."""

from pathlib import Path


def read_report(report_root: Path, requested_name: str) -> str:
    """Read a named report from the configured directory."""
    root = report_root.resolve()
    candidate = (root / requested_name).resolve()
    if candidate.parent != root:
        raise ValueError("requested_name must identify a file in report_root")
    return candidate.read_text(encoding="utf-8")
