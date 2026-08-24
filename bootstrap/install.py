#!/usr/bin/env python3
"""Idempotently install the managed review profile into HERMES_HOME."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, NamedTuple

try:
    import yaml
except ImportError as exc:  # Hermes ships PyYAML; fail clearly on unexpected images.
    raise SystemExit("PyYAML is required in the Hermes image") from exc

SOURCE = Path(__file__).resolve().parent
PROFILES_SOURCE = SOURCE / "profiles"
DEFAULT_PROFILE = "sundsvall-standard"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve()
PROFILE_RECEIPT = ".review-agent-profile.json"
_PROFILE_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ProfileError(ValueError):
    """A selected deployment profile is missing or violates its contract."""


class DeploymentProfile(NamedTuple):
    key: str
    source: Path
    skills: tuple[str, ...]


def _profile_key(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _PROFILE_KEY_RE.fullmatch(value) is None:
        raise ProfileError(f"{label} must use lower-case words separated by hyphens")
    return value


def load_profile(
    key: str, *, required_skills: tuple[str, ...]
) -> DeploymentProfile:
    """Load one trusted profile bundle without merging runtime configuration."""
    resolved_key = _profile_key(key, label="profile")
    source = PROFILES_SOURCE / resolved_key
    manifest_path = source / "profile.json"
    if not manifest_path.is_file():
        available = ", ".join(
            path.name
            for path in sorted(PROFILES_SOURCE.iterdir())
            if path.is_dir() and (path / "profile.json").is_file()
        )
        suffix = f" Available profiles: {available}." if available else ""
        raise ProfileError(f"unknown review profile: {resolved_key}.{suffix}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"profile {resolved_key} has an invalid profile.json") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "skills"}:
        raise ProfileError(
            f"profile {resolved_key} may define only schema_version and skills"
        )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ProfileError(f"profile {resolved_key} uses an unsupported schema version")
    raw_skills = manifest["skills"]
    if not isinstance(raw_skills, list) or not raw_skills:
        raise ProfileError(f"profile {resolved_key} must list at least one reviewed skill")
    skills = tuple(_profile_key(value, label="skill") for value in raw_skills)
    if len(set(skills)) != len(skills):
        raise ProfileError(f"profile {resolved_key} lists a skill more than once")
    missing_skills = [skill for skill in required_skills if skill not in skills]
    if missing_skills:
        raise ProfileError(
            f"profile {resolved_key} is missing managed route skill "
            + ", ".join(missing_skills)
        )
    for required in (source / "SOUL.md", source / "workspace" / "AGENTS.md"):
        if not required.is_file():
            raise ProfileError(f"profile {resolved_key} is missing {required.relative_to(source)}")
    for skill in skills:
        if not (source / "skills" / skill / "SKILL.md").is_file():
            raise ProfileError(f"profile {resolved_key} is missing reviewed skill {skill}")
    return DeploymentProfile(key=resolved_key, source=source, skills=skills)


def installed_profile_receipt() -> tuple[str | None, tuple[str, ...]]:
    path = HERMES_HOME / PROFILE_RECEIPT
    if not path.is_file():
        return None, ()
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(receipt, dict)
            or set(receipt) != {"schema_version", "profile", "skills"}
            or type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != 1
        ):
            raise ProfileError("receipt shape or schema is invalid")
        profile = _profile_key(receipt.get("profile"), label="installed profile")
        raw_skills = receipt.get("skills")
        if not isinstance(raw_skills, list):
            raise ProfileError("installed skills must be a list")
        skills = tuple(
            _profile_key(value, label="installed skill") for value in raw_skills
        )
        if len(set(skills)) != len(skills):
            raise ProfileError("installed skills contain duplicates")
        return profile, skills
    except (OSError, json.JSONDecodeError, ProfileError):
        print(
            "Ignoring invalid installed review profile receipt; "
            "selecting from the flag, environment, or packaged default.",
            file=sys.stderr,
        )
        return None, ()


def deep_merge(existing: Any, managed: Any) -> Any:
    """Merge dictionaries recursively, with managed values taking precedence."""
    if isinstance(existing, dict) and isinstance(managed, dict):
        result = dict(existing)
        for key, value in managed.items():
            result[key] = deep_merge(result.get(key), value) if key in result else value
        return result
    return managed


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def managed_review_skills(config: dict[str, Any]) -> tuple[str, ...]:
    """Return the skills required by the one managed review route."""
    try:
        raw_skills = config["platforms"]["webhook"]["extra"]["routes"][
            "review-agent"
        ]["skills"]
    except (KeyError, TypeError) as exc:
        raise ProfileError("managed review route has no valid skill list") from exc
    if not isinstance(raw_skills, list) or not raw_skills:
        raise ProfileError("managed review route has no valid skill list")
    return tuple(_profile_key(value, label="managed route skill") for value in raw_skills)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def copy_managed_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source,
        target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        help=(
            "Trusted bundle key under bootstrap/profiles. Defaults to "
            "REVIEW_AGENT_PROFILE, the installed receipt, or sundsvall-standard."
        ),
    )
    parser.add_argument(
        "--preserve-soul",
        action="store_true",
        help="Do not replace an existing SOUL.md.",
    )
    parser.add_argument(
        "--force-agents",
        action="store_true",
        help="Replace an existing workspace/AGENTS.md instead of preserving local edits.",
    )
    parser.add_argument(
        "--skip-plugin-enable",
        action="store_true",
        help="Copy the plugin but do not run hermes plugins enable.",
    )
    args = parser.parse_args()

    try:
        managed = load_yaml(SOURCE / "config.yaml")
        installed_profile, previous_skills = installed_profile_receipt()
        profile = load_profile(
            args.profile
            or os.environ.get("REVIEW_AGENT_PROFILE")
            or installed_profile
            or DEFAULT_PROFILE,
            required_skills=managed_review_skills(managed),
        )
    except ProfileError as exc:
        parser.error(str(exc))

    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    (HERMES_HOME / "workspace").mkdir(parents=True, exist_ok=True)
    (HERMES_HOME / "review-memory").mkdir(parents=True, exist_ok=True)

    # Preserve unrelated operator settings while the managed profile remains
    # authoritative for the reviewer route, model, and security boundaries.
    config_path = HERMES_HOME / "config.yaml"
    existing = load_yaml(config_path)
    merged = deep_merge(existing, managed)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_suffix(".yaml.before-review-agent"))
    atomic_write(config_path, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))

    soul_target = HERMES_HOME / "SOUL.md"
    if not soul_target.exists() or not args.preserve_soul:
        if soul_target.exists():
            shutil.copy2(soul_target, HERMES_HOME / "SOUL.md.before-review-agent")
        shutil.copy2(profile.source / "SOUL.md", soul_target)

    agents_target = HERMES_HOME / "workspace" / "AGENTS.md"
    profile_changed = installed_profile is not None and installed_profile != profile.key
    if not agents_target.exists() or args.force_agents or profile_changed:
        if agents_target.exists():
            shutil.copy2(
                agents_target, agents_target.with_suffix(".md.before-review-agent")
            )
        shutil.copy2(profile.source / "workspace" / "AGENTS.md", agents_target)

    receipt_path = HERMES_HOME / PROFILE_RECEIPT
    for skill in previous_skills:
        if skill not in profile.skills:
            target = HERMES_HOME / "skills" / skill
            if target.exists():
                shutil.rmtree(target)
    for skill in profile.skills:
        copy_managed_tree(
            profile.source / "skills" / skill,
            HERMES_HOME / "skills" / skill,
        )
    copy_managed_tree(
        SOURCE / "plugins" / "review_agent_tools",
        HERMES_HOME / "plugins" / "review_agent_tools",
    )

    # Prevent future bundled-skill seeding. Existing bundled skills are not deleted.
    (HERMES_HOME / ".no-bundled-skills").touch(exist_ok=True)
    atomic_write(
        receipt_path,
        json.dumps(
            {"schema_version": 1, "profile": profile.key, "skills": profile.skills},
            indent=2,
        )
        + "\n",
    )

    plugin_dir = HERMES_HOME / "plugins" / "review_agent_tools"
    sys.path.insert(0, str(plugin_dir))
    import memory_db  # type: ignore

    with closing(memory_db.connect()):
        pass

    if not args.skip_plugin_enable:
        result = subprocess.run(
            ["hermes", "plugins", "enable", "review-agent-tools"],
            check=False,
            text=True,
            capture_output=True,
            env={**os.environ, "HERMES_HOME": str(HERMES_HOME)},
        )
        if result.returncode != 0:
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            print(
                "Plugin files were installed, but automatic enablement failed. "
                "Run: hermes plugins enable review-agent-tools",
                file=sys.stderr,
            )
            return result.returncode

    print(f"Installed review profile {profile.key!r} into {HERMES_HOME}")
    print(
        "Next: authenticate with `hermes auth add openai-codex` if needed, "
        "then restart the gateway."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
