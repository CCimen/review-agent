"""Read immutable image build metadata; source checkouts identify as development."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class BuildInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(default="development", min_length=1)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


def read_build_info() -> BuildInfo:
    path = Path(__file__).with_name("_build.json")
    if not path.exists():
        return BuildInfo()
    return BuildInfo.model_validate_json(path.read_bytes())
