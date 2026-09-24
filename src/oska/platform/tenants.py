"""A tenant as a dimension of resolution: what one tenant may change, and how.

A tenant may carry an overlay directory `<platform dir>/tenants/<tenant>/`.

- `connectors.json` has the shape of a pack's bindings. Each binding in it
  changes the binding of the same integration id in every domain the tenant is
  served in, and it may change only where an HTTP binding points: its
  `base_url` and its `headers`. The kind of connector, its operations, its
  fixture, its resilience and anything else stay what the pack declared; an
  entry repeating a declared value is accepted, one changing it is refused.
  The `${NAME}` references in what a tenant changes may read only the
  variables the platform manifest declares for that tenant, and a tenant that
  moves a binding whose declared headers read the environment brings headers of
  its own, so a credential of the domain never follows a binding to an address
  a tenant chose. A binding for an integration no pack of the platform
  declares is refused, because it could only be a mistake.
- `runtime.json` is a fragment merged over the runtime configuration. Only keys
  whose effect stays inside the tenant are accepted: `reply` (wording
  defaults), `discovery.languages` (the languages requests are ranked in) and
  `resilience` (what a connector call may cost). Anything else, such as
  identity, storage, guardrails, catalog or state paths, belongs to the
  platform, and a fragment naming it is refused. The costs in `TENANT_CEILINGS`
  may be lowered by a tenant and never raised past the platform's own value.

A tenant name is a path segment of the state directory, so it is held to the
same shape as every other name and never read as a path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from oska.config import RuntimeConfig
from oska.connectors.registry import environment_references
from oska.contracts.platform import EnvironmentSpec
from oska.platform.errors import TenantOverlayError, UnknownTenant

__all__ = [
    "CONNECTORS_OVERLAY",
    "DEFAULT_TENANT",
    "RUNTIME_OVERLAY",
    "TENANTS_DIR",
    "TENANT_BINDING_KEYS",
    "TENANT_BINDING_KINDS",
    "TENANT_CEILINGS",
    "TENANT_SAFE_KEYS",
    "TenantOverlay",
    "apply_runtime",
    "check_tenant_name",
    "load_overlay",
    "overlay_binding",
]

DEFAULT_TENANT = "default"
TENANTS_DIR = "tenants"
CONNECTORS_OVERLAY = "connectors.json"
RUNTIME_OVERLAY = "runtime.json"
TENANT_SAFE_KEYS: dict[str, frozenset[str] | None] = {
    "reply": None,
    "discovery": frozenset({"languages"}),
    "resilience": None,
}
"""Sections a tenant may change; `None` means every key of the section."""
TENANT_CEILINGS: tuple[tuple[str, str], ...] = (
    ("resilience", "retries"),
    ("resilience", "timeout_seconds"),
    ("resilience", "backoff_seconds"),
    ("resilience", "max_response_bytes"),
    ("reply", "conversation_window"),
)
"""Runtime values a tenant may lower and may not raise past the platform's."""
TENANT_BINDING_KEYS = frozenset({"base_url", "headers"})
"""What a tenant may change in a binding a pack declares."""
TENANT_BINDING_KINDS = frozenset({"http"})
"""The kinds of binding that read `base_url` and `headers` at all."""

_TENANT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class TenantOverlay:
    """What one tenant changes: a runtime fragment and a file of bindings."""

    tenant: str
    runtime: dict
    connectors: Path | None
    integration_ids: frozenset[str]
    bindings: dict = field(default_factory=dict)


def check_tenant_name(tenant: str) -> str:
    """The tenant name, refused when it could not be a single path segment."""
    if not _TENANT.match(tenant):
        raise UnknownTenant(f"{tenant!r} is not a tenant name: lowercase letters, digits, - and _")
    return tenant


def load_overlay(platform_root: Path, tenant: str) -> TenantOverlay:
    """The overlay of one tenant, empty when the tenant carries none."""
    root = Path(platform_root) / TENANTS_DIR / check_tenant_name(tenant)
    fragment: dict = {}
    runtime_path = root / RUNTIME_OVERLAY
    if runtime_path.is_file():
        fragment = _read_object(runtime_path)
        _refuse_unsafe(fragment, runtime_path)
    connectors = root / CONNECTORS_OVERLAY
    integrations: dict = {}
    if connectors.is_file():
        integrations = _read_object(connectors).get("integrations") or {}
        if not isinstance(integrations, dict):
            raise TenantOverlayError(f"{connectors}: expected an object under 'integrations'")
        for integration_id, change in integrations.items():
            if not isinstance(change, dict):
                raise TenantOverlayError(f"{connectors}: {integration_id} is not an object")
    return TenantOverlay(
        tenant=tenant,
        runtime=fragment,
        connectors=connectors if connectors.is_file() else None,
        integration_ids=frozenset(integrations),
        bindings=integrations,
    )


def overlay_binding(
    tenant: str,
    integration_id: str,
    declared: dict,
    change: dict,
    environment: EnvironmentSpec,
) -> dict:
    """A binding a pack declares with what one tenant changes in it, each change checked.

    Only `base_url` and `headers` of a binding that reads them may differ from
    what the pack declared, and what they read from the environment is what the
    tenant's `environment` admits. A new `base_url` under declared headers that
    read the environment needs headers of the tenant's own.
    """
    where = f"tenant {tenant}: {integration_id}"
    kind = declared.get("type")
    if "type" in change and change["type"] != kind:
        raise TenantOverlayError(
            f"{where}: the pack binds it as {kind!r}, and a tenant may not change it to "
            f"{change['type']!r}; a tenant changes only {_binding_keys()}"
        )
    for key, value in change.items():
        if key != "type" and key not in TENANT_BINDING_KEYS and declared.get(key) != value:
            raise TenantOverlayError(
                f"{where}: a tenant may not change {key!r}; it changes only {_binding_keys()}"
            )
    changed = {
        key: change[key]
        for key in sorted(TENANT_BINDING_KEYS)
        if key in change and change[key] != declared.get(key)
    }
    if not changed:
        return dict(declared)
    if kind not in TENANT_BINDING_KINDS:
        raise TenantOverlayError(
            f"{where}: a {kind} binding reads no base_url or headers, "
            "so a tenant has nothing to change in it"
        )
    if "headers" in changed and not isinstance(changed["headers"], dict):
        raise TenantOverlayError(f"{where}: headers is not an object")
    for name in environment_references(list(changed.values())):
        if not environment.allows(name):
            raise TenantOverlayError(
                f"{where}: reads {name} from the environment, which the manifest does not "
                f"declare for tenant {tenant}"
            )
    if (
        "base_url" in changed
        and "headers" not in changed
        and environment_references(declared.get("headers"))
    ):
        raise TenantOverlayError(
            f"{where}: the pack's headers read the environment, and a tenant that moves "
            "the binding to another base_url declares its own headers"
        )
    return {**declared, **changed}


def apply_runtime(
    base: RuntimeConfig, fragment: dict, *, where: str = "the tenant runtime fragment"
) -> RuntimeConfig:
    """The runtime configuration with a tenant's fragment merged over it.

    A value in `TENANT_CEILINGS` above the platform's own is refused, naming the
    key and both values.
    """
    if not fragment:
        return base
    merged = base.model_dump(mode="json")
    for section, values in fragment.items():
        merged[section] = {**merged.get(section, {}), **values}
    try:
        result = RuntimeConfig.model_validate(merged)
    except ValidationError as exc:
        raise TenantOverlayError(f"{where} does not hold up: {exc}") from exc
    for section, key in TENANT_CEILINGS:
        if key not in (fragment.get(section) or {}):
            continue
        wanted = getattr(getattr(result, section), key)
        ceiling = getattr(getattr(base, section), key)
        if wanted > ceiling:
            raise TenantOverlayError(
                f"{where}: {section}.{key} is {wanted}, above the platform's {ceiling}; "
                "a tenant may lower it and may not raise it"
            )
    return result


def _refuse_unsafe(fragment: dict, where: Path) -> None:
    for section, values in fragment.items():
        if section not in TENANT_SAFE_KEYS:
            raise TenantOverlayError(
                f"{where}: a tenant may not change {section!r}; "
                f"it may change {', '.join(_allowed())}"
            )
        if not isinstance(values, dict):
            raise TenantOverlayError(f"{where}: {section!r} is not an object")
        allowed = TENANT_SAFE_KEYS[section]
        if allowed is None:
            continue
        for key in values:
            if key not in allowed:
                raise TenantOverlayError(
                    f"{where}: a tenant may not change {section}.{key}; "
                    f"it may change {', '.join(_allowed())}"
                )


def _binding_keys() -> str:
    return " and ".join(sorted(TENANT_BINDING_KEYS)) + " of an http binding"


def _allowed() -> list[str]:
    return [
        section if keys is None else f"{section}.{key}"
        for section, keys in TENANT_SAFE_KEYS.items()
        for key in (sorted(keys) if keys is not None else [""])
    ]


def _read_object(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TenantOverlayError(f"{path}: cannot be read: {exc}") from exc
    if not isinstance(document, dict):
        raise TenantOverlayError(f"{path}: expected a JSON object")
    return document
