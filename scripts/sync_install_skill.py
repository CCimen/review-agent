#!/usr/bin/env python3
"""Generate agent-specific mirrors from the canonical installation skill."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "skills" / "install-review-agent" / "SKILL.md"
CODEX = ROOT / ".agents" / "skills" / "install-review-agent" / "SKILL.md"
CLAUDE = ROOT / ".claude" / "skills" / "install-review-agent" / "SKILL.md"
FRONTMATTER = re.compile(r"\A---\n(?P<header>.*?)\n---\n(?P<body>.*)\Z", re.DOTALL)


def _rendered() -> dict[Path, str]:
    canonical = CANONICAL.read_text(encoding="utf-8")
    match = FRONTMATTER.fullmatch(canonical)
    if match is None:
        raise ValueError("canonical installation skill has invalid frontmatter")
    claude_header = (
        f"{match.group('header')}\n"
        "disable-model-invocation: true\n"
        "user-invocable: true"
    )
    return {
        CODEX: canonical,
        CLAUDE: f"---\n{claude_header}\n---\n{match.group('body')}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    outputs = _rendered()
    if args.check:
        stale = [
            str(path.relative_to(ROOT))
            for path, expected in outputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != expected
        ]
        if stale:
            raise SystemExit("installation skill mirrors are stale: " + ", ".join(stale))
        print("Installation skill mirrors are current.")
        return 0
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
