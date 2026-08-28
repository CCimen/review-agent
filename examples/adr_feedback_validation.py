"""Disposable fixture for the live ADR-feedback validation PR."""

import subprocess


def run_maintenance(command: str) -> subprocess.CompletedProcess[str]:
    """Run one maintenance command and return its captured result."""
    return subprocess.run(  # noqa: S602
        command,
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )
