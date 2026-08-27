#!/usr/bin/env python3
"""Fail when the release candidate retains a deleted non-App product path."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from typing import NamedTuple

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    Path(".agents"),
    Path(".claude"),
    Path(".env.example"),
    Path(".github/workflows"),
    Path("Dockerfile"),
    Path("PRODUCT.md"),
    Path("README.md"),
    Path("bootstrap"),
    Path("compose.yaml"),
    Path("docs"),
    Path("examples"),
    Path("install"),
    Path("review-learning"),
    Path("scripts"),
    Path("skills"),
    Path("tools"),
    Path("website"),
)
EXCLUDED_PARTS = {
    ".docusaurus",
    ".git",
    "__pycache__",
    "build",
    "node_modules",
}
EXCLUDED_PREFIXES = (Path("docs/goals"),)
EXCLUDED_FILES = {
    Path("scripts/check_app_only_candidate.py"),
    Path("website/package-lock.json"),
    Path("install/package-lock.json"),
}
RESIDUAL_RULES = (
    (
        "legacy GitHub credential",
        re.compile(
            r"\b(?:GITHUB_READ_TOKEN|REVIEW_AGENT_PUBLISH_GH_TOKEN|"
            r"REVIEW_AGENT_FEEDBACK_GH_TOKEN)\b"
        ),
    ),
    (
        "legacy repository allowlist",
        re.compile(r"\bREVIEW_AGENT_ALLOWED_REPOSITORIES\b"),
    ),
    (
        "legacy webhook secret",
        re.compile(
            r"\b(?:REVIEW_AGENT_WEBHOOK_SECRET|"
            r"REVIEW_AGENT_FEEDBACK_WEBHOOK_SECRET)\b"
        ),
    ),
    ("copied Review Agent workflow", re.compile(r"ai-review-request\.ya?ml")),
    (
        "legacy HMAC route",
        re.compile(r"/webhooks/review-agent(?:-feedback)?\b"),
    ),
    ("feedback sidecar", re.compile(r"\bhermes-review-feedback\b")),
    (
        "stale workflow onboarding",
        re.compile(
            r"Install the trusted workflow|GitHub Actions authorizes trusted "
            r"commenters|explicit repository allowlist"
        ),
    ),
    (
        "embedded credential",
        re.compile(
            r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b|"
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        ),
    ),
)


class Finding(NamedTuple):
    path: str
    line: int
    rule: str


def _is_excluded(relative: Path) -> bool:
    return (
        relative in EXCLUDED_FILES
        or any(part in EXCLUDED_PARTS for part in relative.parts)
        or any(relative.is_relative_to(prefix) for prefix in EXCLUDED_PREFIXES)
    )


def candidate_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for relative in SCAN_ROOTS:
        source = root / relative
        if source.is_file():
            paths.append(relative)
            continue
        if source.is_dir():
            for directory, names, filenames in os.walk(source):
                names[:] = sorted(name for name in names if name not in EXCLUDED_PARTS)
                directory_path = Path(directory)
                for filename in sorted(filenames):
                    candidate = (directory_path / filename).relative_to(root)
                    if not _is_excluded(candidate):
                        paths.append(candidate)
    return tuple(sorted(set(paths)))


def scan_residual_paths(root: Path, paths: tuple[Path, ...]) -> list[Finding]:
    findings: list[Finding] = []
    for relative in paths:
        try:
            lines = (root / relative).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            for rule, pattern in RESIDUAL_RULES:
                if pattern.search(line):
                    findings.append(Finding(relative.as_posix(), line_number, rule))
    return findings


def _environment_names(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(name) for name in value}
    if isinstance(value, list):
        return {
            item.partition("=")[0]
            for item in value
            if isinstance(item, str) and item.partition("=")[0]
        }
    return set()


def _secret_names(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    names: set[str] = set()
    for item in value:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict) and isinstance(item.get("source"), str):
            names.add(item["source"])
    return names


def _topology_findings(root: Path) -> list[Finding]:
    relative = Path("compose.yaml")
    try:
        raw = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [Finding(relative.as_posix(), 1, "compose.yaml is missing")]
    except (OSError, UnicodeError, yaml.YAMLError):
        return [Finding(relative.as_posix(), 1, "compose.yaml is not readable YAML")]
    services = raw.get("services") if isinstance(raw, dict) else None
    if not isinstance(services, dict):
        return [Finding(relative.as_posix(), 1, "Compose services are missing")]

    findings: list[Finding] = []
    key_holders: set[str] = set()
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        environment_names = _environment_names(service.get("environment"))
        secret_names = _secret_names(service.get("secrets"))
        has_key_secret = "github_app_private_key" in secret_names
        if "REVIEW_AGENT_GITHUB_APP_PRIVATE_KEY_FILE" in environment_names or has_key_secret:
            key_holders.add(str(name))
        for key in environment_names:
            if "GITHUB" not in key:
                continue
            expected_holder: str | None = None
            if "WEBHOOK_SECRET" in key:
                expected_holder = "review-admission"
            elif any(part in key for part in ("TOKEN", "PRIVATE_KEY", "APP_ID")):
                expected_holder = "review-github-gateway"
            if expected_holder is not None and name != expected_holder:
                findings.append(
                    Finding(
                        relative.as_posix(),
                        1,
                        f"GitHub credential exposed to {name}",
                    )
                )

    if key_holders != {"review-github-gateway"}:
        joined = ", ".join(sorted(key_holders)) or "none"
        findings.append(
            Finding(
                relative.as_posix(),
                1,
                f"App private key holders must be review-github-gateway only; found {joined}",
            )
        )

    gateway = services.get("review-github-gateway")
    if not isinstance(gateway, dict):
        findings.append(Finding(relative.as_posix(), 1, "GitHub gateway service is missing"))
    elif gateway.get("ports") or any(
        str(label).startswith("traefik.http.routers")
        for label in (gateway.get("labels") or [])
    ):
        findings.append(Finding(relative.as_posix(), 1, "GitHub gateway is public"))
    return findings


def check_candidate(root: Path = ROOT) -> list[Finding]:
    findings = scan_residual_paths(root, candidate_paths(root))
    findings.extend(_topology_findings(root))
    for removed in (
        Path(".github/workflows/ai-review-request.yml"),
        Path("examples/github/ai-review-request.yml"),
        Path("scripts/smoke_webhook.py"),
    ):
        if (root / removed).exists():
            findings.append(Finding(removed.as_posix(), 1, "deleted product path exists"))
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    findings = check_candidate(args.root.resolve())
    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.rule}")
        return 1
    print("App-only candidate gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
