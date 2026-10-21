"""Claiming a place on disk for a package, and giving it back when the decision is not recorded.

Applying a change request writes files before the record says so. Two things
must hold. A version is written by exactly one request, even when two are
applied at once: the target directory is created with `os.mkdir`, which fails
when it exists, so the claim and the check are one step the filesystem makes
atomic. And a write that is not recorded leaves nothing behind: the directory
the call created, with what it wrote into it, and the parents it created, are
removed again, and nothing that was there before it.

Both change request stores use this, so the rule is one piece of code.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from oska.contracts.change import ChangeRequestError

__all__ = ["Claim", "claim_target"]


@dataclass
class Claim:
    """A directory this call created, and the parents it had to create to reach it."""

    target: Path
    parents: list[Path] = field(default_factory=list)

    def release(self) -> None:
        """Remove the claimed directory with what was written in it, then the parents it made.

        A parent that is not empty any more is somebody else's by now, and stays.
        """
        shutil.rmtree(self.target, ignore_errors=True)
        for parent in self.parents:
            try:
                parent.rmdir()
            except OSError:
                break


def claim_target(target: Path) -> Claim:
    """Create `target` as a new directory, or refuse because it is already there.

    `target`'s parents are created as needed. The refusal is raised when the
    directory exists, however it came to: a version is never overwritten, and of
    two callers racing for one target exactly one gets it.
    """
    target = Path(target)
    parents = [parent for parent in target.parents if not parent.exists()]
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(target)
    except FileExistsError:
        raise ChangeRequestError(
            f"{target} is already there: a version is never overwritten"
        ) from None
    return Claim(target, parents)
