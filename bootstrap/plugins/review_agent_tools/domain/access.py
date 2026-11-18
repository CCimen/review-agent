"""Fixed platform and team roles for authenticated application access."""

from enum import StrEnum


class Role(StrEnum):
    MEMBER = "member"
    VIEWER = "viewer"
    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def from_flags(
        cls, *, platform_owner: bool, superuser: bool, global_viewer: bool
    ) -> "Role":
        """The platform role of an account; the highest flag set wins."""
        if platform_owner:
            return cls.OWNER
        if superuser:
            return cls.ADMIN
        if global_viewer:
            return cls.VIEWER
        return cls.MEMBER


class TeamRole(StrEnum):
    MAINTAINER = "maintainer"
    VIEWER = "viewer"
