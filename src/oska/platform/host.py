"""Who a domain answers as, in the sentences the catalog already derives.

The greeting, the declared scope and the off-scope reply all end with the one
sentence that names what the runtime can do. In a domain that sentence opens by
naming the host the manifest declares, so every catalog-derived answer says who
is speaking. The name is read from the manifest and nowhere else: a host is
declared, never inferred from the skills or the conversation.
"""

from __future__ import annotations

from oska.catalog.registry import Catalog
from oska.catalog.resolver import CapabilityResolver
from oska.contracts.platform import HostIdentity

__all__ = ["HOST_INTRODUCTION", "HostedResolver"]

HOST_INTRODUCTION = "I am {name}."


class HostedResolver(CapabilityResolver):
    """The catalog wording of one domain, introduced by the host of that domain."""

    def __init__(self, registry: Catalog, host: HostIdentity, *, conjunction: str = "and") -> None:
        super().__init__(registry, conjunction=conjunction)  # type: ignore[arg-type]
        self._host = host

    @property
    def host(self) -> HostIdentity:
        """The host this domain answers as."""
        return self._host

    def declared_scope(self) -> str:
        """The declared scope, opened by the name of the host."""
        introduction = HOST_INTRODUCTION.format(name=self._host.name)
        return f"{introduction} {super().declared_scope()}"
