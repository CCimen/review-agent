#!/usr/bin/env python3
"""Verify a built npm inventory and bind the release copy to its source and image."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    parser.add_argument("lockfile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()
    lock_bytes = args.lockfile.read_bytes()
    lock = json.loads(lock_bytes)
    sbom = json.loads(args.sbom.read_bytes())
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
        parser.error("expected npm's CycloneDX 1.5 inventory")
    packages = {(item["name"], item["version"]) for item in sbom["components"]}
    for name in lock["packages"][""]["dependencies"]:
        version = lock["packages"]["node_modules/" + name]["version"]
        if (name, version) not in packages:
            parser.error(f"frontend inventory is missing {name}@{version}")
    properties = sbom["metadata"].setdefault("properties", [])
    for name, value in (
        ("version", args.version),
        ("source-revision", args.revision),
        ("image", args.image),
        ("platform", args.platform),
        ("package-lock-sha256", hashlib.sha256(lock_bytes).hexdigest()),
    ):
        properties.append({"name": "review-agent:" + name, "value": value})
    args.output.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
