"""Who vouches for a revision, and how a runtime checks that somebody did.

A revision is named by its content, which says what it is and nothing about who
released it. A signature closes that gap: the build signs the manifest, the
manifest names the hash of every file the revision holds, and a runtime that
loads the revision checks both before it serves a line of it.

A signer is anything that satisfies `RevisionSigner`. One ships here,
`hmac-sha256`, with a shared key read from an environment variable. A key
management service plugs in through the same protocol: register a factory under
a new name with `register_signer`, have `sign` send the manifest bytes (or their
digest) to the service and return what it answers, have `verify` ask the
service, or check an asymmetric signature against a public key it holds, and
name that signer in `catalog.signing.signer`. Nothing else changes: the
signature file, the check on load and the refusal are the same whichever signer
produced the bytes.

The key never appears in configuration or in the manifest. Configuration names
the variable that holds it; a missing variable is a refusal, never a quiet
fallback to serving unchecked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from oska.config import SigningConfig

__all__ = [
    "HMAC_SHA256",
    "SIGNATURE_FILENAME",
    "HmacSha256Signer",
    "RevisionSigner",
    "RevisionTrust",
    "RevisionTrustError",
    "register_signer",
    "signature_document",
    "signer_for",
    "signer_names",
]

SIGNATURE_FILENAME = "manifest.sig"
HMAC_SHA256 = "hmac-sha256"


class RevisionTrustError(RuntimeError):
    """A revision cannot be served because its signature does not hold up."""


class RevisionSigner(Protocol):
    """What signs the bytes of a manifest and later checks that signature."""

    @property
    def name(self) -> str:
        """The name this signer is registered under and recorded as."""
        ...

    def sign(self, manifest_bytes: bytes) -> str:
        """A signature over exactly these bytes, as text."""
        ...

    def verify(self, manifest_bytes: bytes, signature: str) -> bool:
        """Whether the signature was made over exactly these bytes by this signer."""
        ...


SignerFactory = Callable[[SigningConfig, Mapping[str, str]], RevisionSigner]


class HmacSha256Signer:
    """A shared-key signature: HMAC-SHA256 over the manifest, base64url without padding."""

    name = HMAC_SHA256

    def __init__(self, key: bytes) -> None:
        if not key:
            raise RevisionTrustError("an HMAC signer needs a key, and it was given none")
        self._key = key

    def sign(self, manifest_bytes: bytes) -> str:
        """The HMAC of the manifest bytes under this key."""
        digest = hmac.new(self._key, manifest_bytes, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def verify(self, manifest_bytes: bytes, signature: str) -> bool:
        """Compare in constant time, so a near miss takes as long as a far one."""
        return hmac.compare_digest(self.sign(manifest_bytes), signature)


def _hmac_from_config(config: SigningConfig, env: Mapping[str, str]) -> RevisionSigner:
    key = str(env.get(config.key_env, ""))
    if not key:
        raise RevisionTrustError(
            f"the {HMAC_SHA256} signer reads its key from {config.key_env}, which is not set"
        )
    return HmacSha256Signer(key.encode("utf-8"))


_SIGNERS: dict[str, SignerFactory] = {HMAC_SHA256: _hmac_from_config}


def register_signer(name: str, factory: SignerFactory) -> None:
    """Make a signer available by name, for configuration to select."""
    if not name.strip():
        raise ValueError("a signer is registered under a name")
    _SIGNERS[name] = factory


def signer_names() -> list[str]:
    """Every signer configuration may name."""
    return sorted(_SIGNERS)


def signer_for(
    config: SigningConfig, env: Mapping[str, str] | None = None, *, name: str = ""
) -> RevisionSigner | None:
    """The signer the configuration names, or nothing when it names none.

    `name` overrides the configured signer, which is how a revision signed by a
    named signer is checked when the configuration names none.
    """
    chosen = name or config.signer
    if not chosen:
        return None
    factory = _SIGNERS.get(chosen)
    if factory is None:
        raise RevisionTrustError(
            f"no signer is registered as {chosen!r}; known: {', '.join(signer_names())}"
        )
    return factory(config, os.environ if env is None else env)


def signature_document(signer: RevisionSigner, manifest_bytes: bytes) -> bytes:
    """The file written beside a manifest: which signer, and what it said."""
    document = {"signer": signer.name, "signature": signer.sign(manifest_bytes)}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


@dataclass(frozen=True)
class RevisionTrust:
    """What a runtime demands of a revision before it serves it.

    `required` refuses an unsigned revision. A signed revision is checked
    whether or not signatures are required: a signature that is there and does
    not hold up is a revision that was changed after it was released.
    """

    config: SigningConfig
    env: Mapping[str, str] | None = None

    @property
    def required(self) -> bool:
        """Whether an unsigned revision is refused."""
        return self.config.required

    def check(self, directory: Path, manifest_bytes: bytes) -> bool:
        """Raise unless the manifest of `directory` may be served; say whether it was signed.

        This checks the manifest. The manifest names the hash of every file the
        revision holds, and checking the files against it is the store's part.
        """
        signature_path = directory / SIGNATURE_FILENAME
        if not signature_path.is_file():
            if self.required:
                raise RevisionTrustError(
                    f"revision {directory.name} is not signed, and signing is required"
                )
            return False
        signer_name, signature = _read_signature(signature_path)
        if self.config.signer and signer_name != self.config.signer:
            raise RevisionTrustError(
                f"revision {directory.name} is signed by {signer_name!r}, "
                f"and this deployment trusts {self.config.signer!r}"
            )
        signer = signer_for(self.config, self.env, name=signer_name)
        if signer is None or not signer.verify(manifest_bytes, signature):
            raise RevisionTrustError(
                f"the signature of revision {directory.name} does not match its manifest"
            )
        return True


def _read_signature(path: Path) -> tuple[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RevisionTrustError(f"{path} cannot be read as a signature: {exc}") from exc
    if not isinstance(document, dict):
        raise RevisionTrustError(f"{path} is not a signature document")
    signer, signature = document.get("signer"), document.get("signature")
    if not isinstance(signer, str) or not isinstance(signature, str) or not signer:
        raise RevisionTrustError(f"{path} does not name a signer and a signature")
    return signer, signature
