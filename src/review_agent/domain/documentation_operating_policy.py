"""Documentation review operating modes, separate from repository-owned rules."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class DocumentationMode(StrEnum):
    OFF = "off"
    MANUAL = "manual"
    AUTOMATIC = "automatic"


ModeSource = Literal["repository", "team", "unassigned"]


@dataclass(frozen=True, slots=True)
class ResolvedDocumentationMode:
    configured_mode: DocumentationMode
    effective_mode: DocumentationMode
    source: ModeSource
    deployment_enabled: bool


def resolve_mode(
    *,
    deployment_enabled: bool,
    team_default: DocumentationMode | None,
    repository_override: DocumentationMode | None,
) -> ResolvedDocumentationMode:
    mode = repository_override or team_default or DocumentationMode.MANUAL
    source: ModeSource = (
        "repository"
        if repository_override is not None
        else "team"
        if team_default is not None
        else "unassigned"
    )
    return ResolvedDocumentationMode(
        configured_mode=mode,
        effective_mode=mode if deployment_enabled else DocumentationMode.OFF,
        source=source,
        deployment_enabled=deployment_enabled,
    )
