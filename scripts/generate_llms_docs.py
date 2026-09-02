#!/usr/bin/env python3
"""Generate public LLM context from the documentation manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from validate_release_tag import is_release_tag


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "website" / "public-documents.json"
STATIC = ROOT / "website" / "static"
BASE_URL = "https://ccimen.github.io/review-agent"
REVISION = "v0.2.0-rc.1"
FRONTMATTER = re.compile(r"\A---\n(?P<header>.*?)\n---\n", re.DOTALL)
TAB_ITEM = re.compile(r'^<TabItem\b[^>]*\blabel="(?P<label>[^"]+)"[^>]*>$')
DIRECTIVE = re.compile(r"^(?P<indent>\s*):::(?P<kind>[a-z]+)\[(?P<title>[^]]+)]$")


class GenerationError(RuntimeError):
    """A public source document cannot be represented safely."""


def _metadata(relative_path: str) -> tuple[dict[str, str], str]:
    path = (ROOT / relative_path).resolve()
    if not path.is_relative_to(ROOT) or not path.is_file():
        raise GenerationError(f"invalid public document: {relative_path}")
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if match is None:
        raise GenerationError(f"missing frontmatter: {relative_path}")
    values: dict[str, str] = {}
    for line in match.group("header").splitlines():
        if not line or line.startswith((" ", "#")) or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip("\"'")
    for required in ("title", "slug", "status", "last_verified"):
        if not values.get(required):
            raise GenerationError(f"{relative_path}: missing {required}")
    return values, text[match.end() :].strip()


def _url(metadata: dict[str, str]) -> str:
    return f"{BASE_URL}/docs/{metadata['slug'].strip('/')}"


def _plain_text(body: str) -> str:
    lines: list[str] = []
    fence: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            lines.append(line)
            continue
        if fence is not None:
            lines.append(line)
            continue
        if stripped.startswith("import ") or stripped in {"</Tabs>", "</TabItem>"}:
            continue
        if stripped == "<Tabs>" or stripped.startswith("<Tabs "):
            continue
        tab_item = TAB_ITEM.fullmatch(stripped)
        if tab_item is not None:
            lines.append(f"### {tab_item.group('label')}")
            continue
        directive = DIRECTIVE.fullmatch(line)
        if directive is not None:
            kind = directive.group("kind").replace("-", " ").title()
            lines.append(
                f"{directive.group('indent')}**{kind}: {directive.group('title')}**"
            )
            continue
        if stripped == ":::":
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def generate(*, revision: str = REVISION) -> tuple[str, str]:
    if not is_release_tag(revision):
        raise GenerationError(f"invalid runtime release tag: {revision}")
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) for item in raw)
        or len(raw) != len(set(raw))
    ):
        raise GenerationError("public-documents.json must be a unique string array")
    documents = [_metadata(path) for path in raw]

    short = [
        "# Review Agent",
        "",
        "Self-hosted, advisory pull-request review with bounded model tools, deterministic authorization, PostgreSQL state, and GitHub App publication.",
        "",
        f"Release state: {revision}",
        "License: EUPL-1.2 (Version 1.2 only). Third-party components retain their original licenses.",
        "Authentication: GitHub App only; install it on explicitly selected repositories, then enable each repository separately.",
        "Model policy: set REVIEW_AGENT_MODEL_PROVIDER, REVIEW_AGENT_MODEL, and REVIEW_AGENT_REASONING_EFFORT to a Hermes-supported route; the documented default uses Codex device-code OAuth.",
        "Trigger: an authorized maintainer posts a new top-level `/review` comment on an open same-repository pull request.",
        "Security boundary: the model has no shell, merge authority, App private key, installation token, or arbitrary GitHub write access.",
        "Feedback when enabled: `/review false-positive` requires the same finding and code context; `/review intentional` also requires the same accepted ADR metadata in the current base snapshot. `/review feedback scope` and `/review feedback missed` record evidence for gated improvement without silently changing live behavior.",
        "Repository guidance: repositories may opt into `.review-agent/config.toml`, one optional `instructions.md`, explicitly ordered `context/**/*.md`, and typed ADRs under `.review-agent/decisions/`; all are read from the exact pull-request base commit and cannot weaken the deployment safety contract.",
        "",
        "## Start here",
        "",
        f"- Quick start: {BASE_URL}/docs/getting-started",
        f"- Deployment: {BASE_URL}/docs/deployment",
        f"- GitHub App setup: {BASE_URL}/docs/github-app-pilot",
        f"- AI-assisted setup: {BASE_URL}/docs/ai-assisted-setup",
        f"- Operations: {BASE_URL}/docs/operations",
        f"- Feedback and design decisions: {BASE_URL}/docs/feedback-and-decisions",
        f"- Repository context: {BASE_URL}/docs/repository-context",
        f"- Security: {BASE_URL}/docs/security",
        "",
        "## Coding-agent handoff",
        "",
        "If no source checkout was provided, clone this repository and check out the exact runtime release named above. Then use the versioned repository-local `skills/install-review-agent/SKILL.md`; Codex and Claude Code mirrors are included, so do not install a floating global copy. Read only the platform-relevant documents above, prepare and validate the non-secret installation plan, and stop only for owner approvals, protected secret placement, DNS, model login, deployment approval, and the first live `/review`. On Dokploy, use Deploy when the source revision changes and verify the completed deployment commit. Finish with doctor, inventory, dry-run, and live-review evidence; report unknowns instead of guessing.",
        "",
        "## Operator interface",
        "",
        "Source checkout prerequisite: create `.venv` and install `requirements.txt` with `.venv/bin/python`. Run `.venv/bin/python tools/review_agent_admin.py capabilities` and `.venv/bin/python tools/review_agent_admin.py preflight`. Validate an optional repository package with `.venv/bin/python tools/review_agent_admin.py repository-context validate <repository-root>`; this command is offline and prints hashes, paths, and status rather than file bodies. After deployment, run `review-agent-admin doctor`, inventory, activation, and `smoke-test --dry-run` inside the documented service container. These commands return bounded JSON except where the command documents another default.",
        "",
        "Quality reporting: run `review-agent-memory quality --days 30` globally or add `--repo owner/repository`. It reports explicit signals beside their denominators and keeps the current triage backlog visible. Missed-issue coach input requires explicit operator triage; a coding agent must not choose the status or target owner.",
        "",
        f"Full public documentation: {BASE_URL}/llms-full.txt",
        "Source: https://github.com/CCimen/review-agent",
        "",
    ]

    full = [
        "# Review Agent: full public documentation",
        "",
        "Generated from the live documentation sources listed in `website/public-documents.json`. Internal goals, review notes, and private learning data are excluded.",
        f"Runtime release selected by `llms.txt`: {revision}",
        "",
    ]
    for metadata, body in documents:
        full.extend(
            [
                "---",
                "",
                f"Title: {metadata['title']}",
                f"Canonical URL: {_url(metadata)}",
                f"Status: {metadata['status']}",
                f"Last verified: {metadata['last_verified']}",
                "",
                _plain_text(body),
                "",
            ]
        )
    return "\n".join(short), "\n".join(full)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    index, full = generate()
    outputs = {
        STATIC / "llms.txt": index,
        STATIC / "llms-full.txt": full,
    }
    if args.check:
        stale = [
            str(path.relative_to(ROOT))
            for path, expected in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != expected
        ]
        if stale:
            raise SystemExit("generated LLM documentation is stale: " + ", ".join(stale))
        print("Generated LLM documentation is current.")
        return 0
    STATIC.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
