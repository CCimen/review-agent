"""Fixed platform and team roles for authenticated application access."""

from enum import StrEnum


class Role(StrEnum):
    MEMBER = "member"
    VIEWER = "viewer"
    ADMIN = "admin"
    OWNER = "owner"


class TeamRole(StrEnum):
    MAINTAINER = "maintainer"
    VIEWER = "viewer"
