"""Disposable fixture for the live ADR-feedback validation PR."""

import subprocess
import sys


def run_maintenance(command: str) -> subprocess.CompletedProcess[str]:
    """Run one maintenance command and return its captured result."""
    return subprocess.run(  # noqa: S602
        command,
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )


def main(arguments: list[str]) -> int:
    """Run the command supplied by the caller."""
    run_maintenance(arguments[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
