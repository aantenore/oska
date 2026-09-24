"""What a platform is made of: a runtime, the packs it serves, and the domains it answers as.

A pack is a versioned directory of declarations for one business area. A
platform manifest names the packs an organization deploys, groups them into
domains, and says who the runtime answers as in each. Nothing here runs: these
are the contracts the runtime, the kit and the conformance checks share.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

__all__ = [
    "ChannelSpec",
    "DomainSpec",
    "EnvironmentSpec",
    "HostIdentity",
    "PackDependency",
    "PackManifest",
    "PackProvides",
    "PackRef",
    "PlatformManifest",
    "StorageSpec",
    "TenantSpec",
]

_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _name(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError(f"{value!r} is not a name: lowercase letters, digits, - and _")
    return value


class HostIdentity(BaseModel):
    """Who the runtime answers as in a domain. Declared, never inferred."""

    name: str
    voice: str = ""
    languages: list[str] = Field(default_factory=list)


class PackDependency(BaseModel):
    """Another pack this one needs, and the versions it accepts."""

    pack: str
    version: str = "*"


class PackProvides(BaseModel):
    """What a pack claims to declare; checked against its skills."""

    entities: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)


class PackManifest(BaseModel):
    """Contents of `pack.yaml`: one business area, versioned and self-contained.

    The layout under the manifest is fixed: `skills/`, `configs/connectors.json`,
    `fixtures/`, `scenarios/`, `evaluation/`, and an optional `guardrails.json`
    whose entries are added to the platform catalog. `synonyms` maps how people
    say a thing to how the catalog names it, so the runtime keeps no word list
    of its own.
    """

    name: str
    version: str
    description: str = Field(min_length=10, max_length=300)
    requires: dict[str, str] = Field(default_factory=dict)
    depends_on: list[PackDependency] = Field(default_factory=list)
    provides: PackProvides = Field(default_factory=PackProvides)
    host: HostIdentity | None = None
    synonyms: dict[str, str] = Field(default_factory=dict)
    level: int = Field(default=1, ge=1, le=3)

    _check_name = field_validator("name")(classmethod(lambda cls, v: _name(v)))

    @field_validator("version")
    @classmethod
    def _semver(cls, value: str) -> str:
        if not _SEMVER.match(value):
            raise ValueError(f"version {value!r} is not MAJOR.MINOR.PATCH")
        return value


class EnvironmentSpec(BaseModel):
    """Which environment variables a set of bindings may read with `${NAME}`.

    `names` admits each variable it lists; `prefix`, when set, admits every
    variable whose name starts with it. A platform binding that reads any other
    variable is refused when the platform is assembled. Nothing declared means
    nothing may be read.
    """

    names: list[str] = Field(default_factory=list)
    prefix: str = ""

    @field_validator("names")
    @classmethod
    def _variables(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _VARIABLE.match(name):
                raise ValueError(f"{name!r} is not an environment variable name")
        return value

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if value and not _VARIABLE.match(value):
            raise ValueError(f"{value!r} cannot start an environment variable name")
        return value

    def allows(self, name: str) -> bool:
        """Whether a binding under this declaration may read the variable."""
        return name in self.names or (bool(self.prefix) and name.startswith(self.prefix))


class TenantSpec(BaseModel):
    """What a platform manifest declares about one tenant.

    `environment` is what the tenant's own binding changes may read: the
    `base_url` and `headers` its overlay puts on a binding a pack declares.
    """

    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)


class PackRef(BaseModel):
    """A pack a platform deploys: where it is, and the version it expects."""

    path: Path
    version: str = "*"


class DomainSpec(BaseModel):
    """One scope a platform answers in: its packs, its host and its tenants.

    A session belongs to one domain for its whole life. A skill of one domain is
    never ranked, offered or executed in another. `environment` is what the
    bindings of the domain's packs may read from the environment.
    """

    name: str
    packs: list[str]
    host: HostIdentity
    tenants: list[str] = Field(default_factory=lambda: ["default"])
    presentation: bool = False
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)

    _check_name = field_validator("name")(classmethod(lambda cls, v: _name(v)))


class ChannelSpec(BaseModel):
    """Which channels a platform opens."""

    cli: bool = True
    http: bool = True


class StorageSpec(BaseModel):
    """Where sessions and receipts are kept. The address comes from the environment."""

    backend: Literal["sqlite", "postgres"] = "sqlite"
    url_env: str = "OSKA_DATABASE_URL"


class PlatformManifest(BaseModel):
    """Contents of `platform.yaml`: what an organization deploys.

    `tenants` declares, per tenant a domain serves, what that tenant may read
    from the environment; a tenant it does not name reads nothing.
    """

    name: str
    packs: dict[str, PackRef]
    domains: list[DomainSpec]
    default_domain: str = ""
    channels: ChannelSpec = Field(default_factory=ChannelSpec)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    target_level: int = Field(default=2, ge=1, le=3)
    tenants: dict[str, TenantSpec] = Field(default_factory=dict)

    _check_name = field_validator("name")(classmethod(lambda cls, v: _name(v)))

    @model_validator(mode="after")
    def _consistent(self) -> PlatformManifest:
        names = [domain.name for domain in self.domains]
        if not names:
            raise ValueError("a platform declares at least one domain")
        if len(set(names)) != len(names):
            raise ValueError("domain names must be unique")
        for domain in self.domains:
            unknown = [pack for pack in domain.packs if pack not in self.packs]
            if unknown:
                raise ValueError(f"domain {domain.name!r} names undeclared packs: {unknown}")
        if self.default_domain and self.default_domain not in names:
            raise ValueError(f"default domain {self.default_domain!r} is not declared")
        served = {tenant for domain in self.domains for tenant in domain.tenants}
        unserved = sorted(set(self.tenants) - served)
        if unserved:
            raise ValueError(f"tenants {unserved} are declared and no domain serves them")
        return self

    def tenant(self, name: str) -> TenantSpec:
        """What the manifest declares about a tenant; nothing when it names none."""
        return self.tenants.get(name) or TenantSpec()

    def domain(self, name: str | None = None) -> DomainSpec:
        """The named domain, or the default one when no name is given."""
        wanted = name or self.default_domain or self.domains[0].name
        for domain in self.domains:
            if domain.name == wanted:
                return domain
        raise KeyError(f"unknown domain {wanted!r}")
