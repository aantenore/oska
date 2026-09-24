"""A platform: packs grouped into domains, served for tenants from one process."""

from oska.platform.errors import (
    DomainMismatch,
    ManifestError,
    PackError,
    PlatformError,
    TenantMismatch,
    TenantOverlayError,
    UnknownDomain,
    UnknownTenant,
)
from oska.platform.manifest import load_platform
from oska.platform.packs import Pack, check_provides, load_pack, resolve_dependencies
from oska.platform.runtime import Platform

__all__ = [
    "DomainMismatch",
    "ManifestError",
    "Pack",
    "PackError",
    "Platform",
    "PlatformError",
    "TenantMismatch",
    "TenantOverlayError",
    "UnknownDomain",
    "UnknownTenant",
    "check_provides",
    "load_pack",
    "load_platform",
    "resolve_dependencies",
]
