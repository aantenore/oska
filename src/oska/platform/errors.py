"""What goes wrong when a platform is assembled or asked to serve a turn."""

from __future__ import annotations

__all__ = [
    "DomainMismatch",
    "ManifestError",
    "PackError",
    "PlatformError",
    "TenantMismatch",
    "TenantOverlayError",
    "UnknownDomain",
    "UnknownTenant",
]


class PlatformError(ValueError):
    """A platform, a pack or a request to either does not hold up."""


class PackError(PlatformError):
    """A pack is missing a part of its layout, or claims what it does not declare."""


class ManifestError(PlatformError):
    """A platform manifest cannot be read, or names something it cannot have."""


class TenantOverlayError(PlatformError):
    """A tenant overlay changes something a tenant is not allowed to change."""


class UnknownDomain(PlatformError):
    """A request names a domain the platform does not declare."""


class UnknownTenant(PlatformError):
    """A request names a tenant the domain does not serve."""


class DomainMismatch(PlatformError):
    """A session bound to one domain is asked to run a turn in another."""


class TenantMismatch(DomainMismatch):
    """A session bound to one tenant is asked to run a turn as another.

    A kind of `DomainMismatch`, so a channel that refuses a session asked in the
    wrong scope refuses this one too, with its own wording.
    """
