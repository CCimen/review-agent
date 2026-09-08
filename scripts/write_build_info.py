#!/usr/bin/env python3
"""Bake the release workflow's version and source revision into an image."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from validate_release_tag import is_release_tag


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    if args.version != "development" and not is_release_tag(args.version):
        parser.error("version must be development or a release tag")
    revision = None if args.revision == "unknown" else args.revision
    if revision is not None and re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        parser.error("revision must be a full lowercase Git SHA or unknown")
    if args.version != "development" and revision is None:
        parser.error("a release version requires a source revision")
    args.output.write_text(
        json.dumps({"version": args.version, "revision": revision}) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
