"""Immutable identity of the installed reviewer behavior."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
from typing import cast, Protocol


RECEIPT_NAME = ".review-agent-profile.json"
PROFILE_MANIFEST_NAME = "review-agent-profile.json"
SCHEMA_VERSION = 2
_PINNED_IMAGE_RE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
_PROFILE_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Hermes configuration reference, validated again by the installed runtime.
REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


class ReviewContractError(ValueError):
    """The installed reviewer does not match its signed-off receipt."""


class _YamlModule(Protocol):
    def safe_load(self, stream: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ContractFile:
    path: str
    sha256: str

    def to_json(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class ReviewContract:
    profile: str
    hermes_image: str
    model_provider: str
    model: str
    reasoning_effort: str
    plugin_result_max_chars: int
    profile_bundle_sha256: str
    managed_config_sha256: str
    engine_bundle_sha256: str
    sha256: str

    def behavior_json(self) -> dict[str, object]:
        return {
            "engine_bundle_sha256": self.engine_bundle_sha256,
            "hermes_image": self.hermes_image,
            "managed_config_sha256": self.managed_config_sha256,
            "model": self.model,
            "model_provider": self.model_provider,
            "plugin_result_max_chars": self.plugin_result_max_chars,
            "profile": self.profile,
            "profile_bundle_sha256": self.profile_bundle_sha256,
            "reasoning_effort": self.reasoning_effort,
        }

    def to_json(self) -> dict[str, object]:
        return {**self.behavior_json(), "sha256": self.sha256}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReviewContractError(f"installed review contract has invalid {field}")
    return value


def _text(mapping: object, *keys: str) -> str:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            raise ReviewContractError(f"installed config has no {'.'.join(keys)}")
        current = cast(dict[str, object], current).get(key)
    if not isinstance(current, str) or not current.strip():
        raise ReviewContractError(f"installed config has no {'.'.join(keys)}")
    return current.strip()


def _positive_int(mapping: object, *keys: str) -> int:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            raise ReviewContractError(f"installed config has no {'.'.join(keys)}")
        current = cast(dict[str, object], current).get(key)
    if type(current) is not int or current < 1:
        raise ReviewContractError(f"installed config has invalid {'.'.join(keys)}")
    return current


def _load_config(path: Path) -> object:
    try:
        yaml_module = cast(_YamlModule, importlib.import_module("yaml"))
        content = path.read_text(encoding="utf-8")
    except (ImportError, OSError) as exc:
        raise ReviewContractError("managed reviewer config is missing or invalid") from exc
    try:
        return yaml_module.safe_load(content)
    except Exception as exc:  # PyYAML exceptions have no stable shared base protocol.
        raise ReviewContractError("managed reviewer config is missing or invalid") from exc


def _deployment_text(name: str, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value or not value.isprintable():
        raise ReviewContractError(f"{name} must be one non-empty printable value")
    return value


def render_managed_config(path: Path) -> tuple[object, bytes]:
    """Apply operator-owned model settings to the managed config."""
    try:
        yaml_module = cast(_YamlModule, importlib.import_module("yaml"))
        source = path.read_text(encoding="utf-8")
        base = yaml_module.safe_load(source)
    except (ImportError, OSError) as exc:
        raise ReviewContractError(
            "managed reviewer config is missing or invalid"
        ) from exc
    except Exception as exc:  # PyYAML exceptions have no stable shared base protocol.
        raise ReviewContractError(
            "managed reviewer config is missing or invalid"
        ) from exc

    default_provider = _text(base, "model", "provider")
    default_model = _text(base, "model", "default")
    default_effort = _text(base, "agent", "reasoning_effort")
    provider = _deployment_text("REVIEW_AGENT_MODEL_PROVIDER", default_provider)
    model = _deployment_text("REVIEW_AGENT_MODEL", default_model)
    effort = (
        os.environ.get("REVIEW_AGENT_REASONING_EFFORT", default_effort).strip().lower()
    )
    if effort not in REASONING_EFFORTS:
        choices = ", ".join(sorted(REASONING_EFFORTS))
        raise ReviewContractError(
            f"REVIEW_AGENT_REASONING_EFFORT must be one of: {choices}"
        )

    provider_line = f"  provider: {default_provider}\n"
    model_line = f"  default: {default_model}\n"
    effort_line = f"  reasoning_effort: {default_effort}\n"
    if any(
        source.count(line) != 1 for line in (provider_line, model_line, effort_line)
    ):
        raise ReviewContractError("managed reviewer config template is invalid")
    rendered = (
        source.replace(provider_line, f"  provider: {json.dumps(provider)}\n", 1)
        .replace(model_line, f"  default: {json.dumps(model)}\n", 1)
        .replace(
            effort_line,
            f"  reasoning_effort: {effort}\n",
            1,
        )
    )
    try:
        config = yaml_module.safe_load(rendered)
    except Exception as exc:  # PyYAML exceptions have no stable shared base protocol.
        raise ReviewContractError(
            "managed reviewer config is missing or invalid"
        ) from exc
    return config, rendered.encode("utf-8")


def _file(path: Path, logical_path: str) -> ContractFile:
    if not path.is_file():
        raise ReviewContractError(f"reviewer file is missing: {logical_path}")
    return ContractFile(path=logical_path, sha256=_digest(path.read_bytes()))


def _tree(path: Path, logical_root: str) -> tuple[ContractFile, ...]:
    if not path.is_dir():
        raise ReviewContractError(f"reviewer directory is missing: {logical_root}")
    files = (
        item
        for item in path.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix not in {".pyc", ".pyo"}
    )
    return tuple(
        _file(item, f"{logical_root}/{item.relative_to(path).as_posix()}")
        for item in sorted(files, key=lambda value: value.relative_to(path).as_posix())
    )


def _bundle_hash(files: tuple[ContractFile, ...]) -> str:
    canonical = json.dumps(
        [item.to_json() for item in files], separators=(",", ":"), sort_keys=True
    )
    return _digest(canonical.encode("utf-8"))


def _installed_files(
    home: Path, skills: tuple[str, ...]
) -> tuple[tuple[ContractFile, ...], tuple[ContractFile, ...], tuple[ContractFile, ...]]:
    profile_files = (
        _file(home / PROFILE_MANIFEST_NAME, "profile/profile.json"),
        _file(home / "SOUL.md", "profile/SOUL.md"),
        _file(home / "workspace" / "AGENTS.md", "profile/workspace/AGENTS.md"),
        *(
            item
            for skill in skills
            for item in _tree(home / "skills" / skill, f"profile/skills/{skill}")
        ),
    )
    config_files = (_file(home / "config.yaml", "config.yaml"),)
    engine_files = _tree(
        home / "plugins" / "review_agent_tools", "plugins/review_agent_tools"
    )
    return profile_files, config_files, engine_files


def _profile_skills(manifest_path: Path) -> tuple[str, ...]:
    try:
        raw = cast(
            object,
            json.loads(manifest_path.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewContractError("packaged review profile is missing or invalid") from exc
    if not isinstance(raw, dict):
        raise ReviewContractError("packaged review profile has an invalid shape")
    raw_mapping = cast(dict[str, object], raw)
    if set(raw_mapping) != {"schema_version", "skills"}:
        raise ReviewContractError("packaged review profile has an invalid shape")
    raw_skills = raw_mapping.get("skills")
    if raw_mapping.get("schema_version") != 1 or not isinstance(raw_skills, list):
        raise ReviewContractError("packaged review profile has an unsupported schema")
    skills_objects = cast(list[object], raw_skills)
    if any(
        not isinstance(skill, str) or _PROFILE_KEY_RE.fullmatch(skill) is None
        for skill in skills_objects
    ):
        raise ReviewContractError("packaged review profile has invalid skills")
    skills = tuple(cast(list[str], skills_objects))
    if not skills or len(set(skills)) != len(skills):
        raise ReviewContractError("packaged review profile has invalid skills")
    return skills


def _packaged_files(
    source: Path,
    profile: str,
    skills: tuple[str, ...],
    *,
    config_bytes: bytes,
) -> tuple[
    tuple[ContractFile, ...], tuple[ContractFile, ...], tuple[ContractFile, ...]
]:
    profile_source = source / "profiles" / profile
    profile_files = (
        _file(profile_source / "profile.json", "profile/profile.json"),
        _file(profile_source / "SOUL.md", "profile/SOUL.md"),
        _file(
            profile_source / "workspace" / "AGENTS.md",
            "profile/workspace/AGENTS.md",
        ),
        *(
            item
            for skill in skills
            for item in _tree(
                profile_source / "skills" / skill, f"profile/skills/{skill}"
            )
        ),
    )
    config_files = (ContractFile(path="config.yaml", sha256=_digest(config_bytes)),)
    engine_files = _tree(
        source / "plugins" / "review_agent_tools", "plugins/review_agent_tools"
    )
    return profile_files, config_files, engine_files


def _build_contract(
    files: tuple[
        tuple[ContractFile, ...],
        tuple[ContractFile, ...],
        tuple[ContractFile, ...],
    ],
    *,
    profile: str,
    hermes_image: str,
    config: object,
) -> ReviewContract:
    image = hermes_image.strip()
    if _PINNED_IMAGE_RE.fullmatch(image) is None:
        raise ReviewContractError("REVIEW_AGENT_HERMES_IMAGE must be digest-pinned")
    profile_files, config_files, engine_files = files
    profile_hash = _bundle_hash(profile_files)
    config_hash = _bundle_hash(config_files)
    engine_hash = _bundle_hash(engine_files)
    values: dict[str, object] = {
        "engine_bundle_sha256": engine_hash,
        "hermes_image": image,
        "managed_config_sha256": config_hash,
        "model": _text(config, "model", "default"),
        "model_provider": _text(config, "model", "provider"),
        "plugin_result_max_chars": _positive_int(
            config,
            "plugins",
            "entries",
            "review-agent-tools",
            "settings",
            "result_max_chars",
        ),
        "profile": profile,
        "profile_bundle_sha256": profile_hash,
        "reasoning_effort": _text(config, "agent", "reasoning_effort"),
    }
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ReviewContract(
        profile=profile,
        hermes_image=image,
        model_provider=cast(str, values["model_provider"]),
        model=cast(str, values["model"]),
        reasoning_effort=cast(str, values["reasoning_effort"]),
        plugin_result_max_chars=cast(int, values["plugin_result_max_chars"]),
        profile_bundle_sha256=profile_hash,
        managed_config_sha256=config_hash,
        engine_bundle_sha256=engine_hash,
        sha256=_digest(canonical.encode("utf-8")),
    )


def build_contract(
    home: Path,
    *,
    profile: str,
    skills: tuple[str, ...],
    hermes_image: str,
    config: object,
) -> ReviewContract:
    """Derive the exact installed behavior from files and managed configuration."""
    return _build_contract(
        _installed_files(home, skills),
        profile=profile,
        hermes_image=hermes_image,
        config=config,
    )


def load_packaged_contract(
    profile: str,
    source: Path | None = None,
    *,
    hermes_image: str | None = None,
) -> ReviewContract:
    """Derive admission identity from the immutable reviewer files in its image."""
    if _PROFILE_KEY_RE.fullmatch(profile) is None:
        raise ReviewContractError("configured review profile is invalid")
    resolved_source = (source or Path("/opt/review-agent-bootstrap")).resolve()
    profile_source = resolved_source / "profiles" / profile
    skills = _profile_skills(profile_source / "profile.json")
    config, config_bytes = render_managed_config(resolved_source / "config.yaml")
    return _build_contract(
        _packaged_files(
            resolved_source,
            profile,
            skills,
            config_bytes=config_bytes,
        ),
        profile=profile,
        hermes_image=(
            hermes_image
            if hermes_image is not None
            else os.environ.get("REVIEW_AGENT_HERMES_IMAGE", "")
        ),
        config=config,
    )


def _contract_from_json(value: object) -> ReviewContract:
    if not isinstance(value, dict):
        raise ReviewContractError("installed contract must be an object")
    raw = cast(dict[str, object], value)
    expected_keys = {
        "engine_bundle_sha256",
        "hermes_image",
        "managed_config_sha256",
        "model",
        "model_provider",
        "plugin_result_max_chars",
        "profile",
        "profile_bundle_sha256",
        "reasoning_effort",
        "sha256",
    }
    if set(raw) != expected_keys:
        raise ReviewContractError("installed review contract has an invalid shape")
    text_fields = {
        field: raw[field]
        for field in (
            "hermes_image",
            "model",
            "model_provider",
            "profile",
            "reasoning_effort",
        )
    }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in text_fields.values()
    ):
        raise ReviewContractError("installed review contract has invalid text fields")
    image = cast(str, text_fields["hermes_image"])
    if _PINNED_IMAGE_RE.fullmatch(image) is None:
        raise ReviewContractError("installed review contract has an invalid Hermes image")
    result_limit = raw["plugin_result_max_chars"]
    if type(result_limit) is not int or result_limit < 1:
        raise ReviewContractError(
            "installed review contract has an invalid plugin result limit"
        )
    profile_hash = _sha256(
        raw["profile_bundle_sha256"], field="profile bundle digest"
    )
    config_hash = _sha256(
        raw["managed_config_sha256"], field="managed config digest"
    )
    engine_hash = _sha256(
        raw["engine_bundle_sha256"], field="engine bundle digest"
    )
    contract = ReviewContract(
        profile=cast(str, text_fields["profile"]),
        hermes_image=image,
        model_provider=cast(str, text_fields["model_provider"]),
        model=cast(str, text_fields["model"]),
        reasoning_effort=cast(str, text_fields["reasoning_effort"]),
        plugin_result_max_chars=result_limit,
        profile_bundle_sha256=profile_hash,
        managed_config_sha256=config_hash,
        engine_bundle_sha256=engine_hash,
        sha256=_sha256(raw["sha256"], field="contract digest"),
    )
    expected_hash = _digest(
        json.dumps(
            contract.behavior_json(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    if contract.sha256 != expected_hash:
        raise ReviewContractError("installed review contract digest does not match")
    return contract


def write_receipt(
    home: Path,
    *,
    profile: str,
    skills: tuple[str, ...],
    hermes_image: str,
    config: object,
) -> ReviewContract:
    contract = build_contract(
        home,
        profile=profile,
        skills=skills,
        hermes_image=hermes_image,
        config=config,
    )
    profile_files, config_files, engine_files = _installed_files(home, skills)
    payload = {
        "contract": contract.to_json(),
        "files": [
            item.to_json()
            for item in (*profile_files, *config_files, *engine_files)
        ],
        "schema_version": SCHEMA_VERSION,
        "skills": list(skills),
    }
    temporary = home / f"{RECEIPT_NAME}.tmp"
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(home / RECEIPT_NAME)
    return contract


def load_installed_contract(home: Path | None = None) -> ReviewContract:
    """Read and verify the receipt against every installed behavior file."""
    resolved_home = (
        home or Path(os.environ.get("HERMES_HOME", "/opt/data"))
    ).resolve()
    try:
        raw_object = cast(
            object,
            json.loads((resolved_home / RECEIPT_NAME).read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewContractError(
            "installed review contract receipt is missing or invalid"
        ) from exc
    if not isinstance(raw_object, dict):
        raise ReviewContractError(
            "installed review contract receipt has an invalid shape"
        )
    raw = cast(dict[str, object], raw_object)
    if set(raw) != {"contract", "files", "schema_version", "skills"}:
        raise ReviewContractError(
            "installed review contract receipt has an invalid shape"
        )
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ReviewContractError(
            "installed review contract receipt has an unsupported schema"
        )
    skills_raw = raw.get("skills")
    contract_raw = raw.get("contract")
    files_raw = raw.get("files")
    if not isinstance(skills_raw, list):
        raise ReviewContractError(
            "installed review contract receipt has an invalid shape"
        )
    skills_objects = cast(list[object], skills_raw)
    if (
        any(not isinstance(item, str) or not item for item in skills_objects)
        or not isinstance(files_raw, list)
    ):
        raise ReviewContractError(
            "installed review contract receipt has an invalid shape"
        )
    if len(set(cast(list[str], skills_objects))) != len(skills_objects):
        raise ReviewContractError("installed review contract skills contain duplicates")
    contract = _contract_from_json(contract_raw)
    installed_skills = _profile_skills(resolved_home / PROFILE_MANIFEST_NAME)
    receipt_skills = tuple(cast(list[str], skills_objects))
    if installed_skills != receipt_skills:
        raise ReviewContractError(
            "installed reviewer skills do not match the profile manifest"
        )
    profile_files, config_files, engine_files = _installed_files(
        resolved_home,
        receipt_skills,
    )
    actual_files = [
        item.to_json() for item in (*profile_files, *config_files, *engine_files)
    ]
    if (
        actual_files != files_raw
        or contract.profile_bundle_sha256 != _bundle_hash(profile_files)
        or contract.managed_config_sha256 != _bundle_hash(config_files)
        or contract.engine_bundle_sha256 != _bundle_hash(engine_files)
    ):
        raise ReviewContractError(
            "installed reviewer files or config do not match the receipt"
        )
    expected = _build_contract(
        (profile_files, config_files, engine_files),
        profile=contract.profile,
        hermes_image=os.environ.get("REVIEW_AGENT_HERMES_IMAGE", ""),
        config=_load_config(resolved_home / "config.yaml"),
    )
    if contract != expected:
        raise ReviewContractError(
            "installed reviewer behavior does not match the receipt"
        )
    return contract


def resolved_config(contract: ReviewContract) -> dict[str, object]:
    """Return resolved_config schema v2 for one exact review subject."""
    return {"profile": contract.profile, "review_contract": contract.to_json()}


def require_matching_resolved_config(value: object, installed: ReviewContract) -> None:
    if value != resolved_config(installed):
        raise ReviewContractError(
            "queued review contract does not match the installed reviewer"
        )
