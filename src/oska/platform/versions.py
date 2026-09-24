"""Version ranges a pack uses to say which versions of something it accepts.

A range is one or more clauses joined by commas, and a version satisfies it
when it satisfies every clause. A clause is `*` (anything), an exact version,
or an operator (`>=`, `<=`, `>`, `<`, `==`, `=`) followed by a version. A
version is `MAJOR.MINOR.PATCH`; a clause may leave out the trailing parts, which
read as zero, so `>=0.2` means `>=0.2.0`.
"""

from __future__ import annotations

import re

__all__ = ["parse_version", "satisfies"]

_CLAUSE = re.compile(r"^(>=|<=|==|=|>|<)?\s*(\d+(?:\.\d+){0,2})$")
_COMPARE = {
    ">=": lambda left, right: left >= right,
    "<=": lambda left, right: left <= right,
    ">": lambda left, right: left > right,
    "<": lambda left, right: left < right,
    "==": lambda left, right: left == right,
    "=": lambda left, right: left == right,
}
ANY = "*"


def parse_version(value: str) -> tuple[int, int, int]:
    """A version as three integers. Missing trailing parts read as zero."""
    parts = value.strip().split(".")
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        raise ValueError(f"{value!r} is not a version: MAJOR.MINOR.PATCH")
    numbers = [int(part) for part in parts] + [0] * (3 - len(parts))
    return numbers[0], numbers[1], numbers[2]


def satisfies(version: str, spec: str) -> bool:
    """Whether a version is inside a range. A range that cannot be read raises."""
    held = parse_version(version)
    clauses = [clause.strip() for clause in spec.split(",")]
    if not any(clauses):
        raise ValueError("an empty version range accepts nothing; write * to accept anything")
    for clause in clauses:
        if clause == ANY:
            continue
        match = _CLAUSE.match(clause)
        if match is None:
            raise ValueError(f"{clause!r} is not a version clause")
        operator = match.group(1) or "=="
        if not _COMPARE[operator](held, parse_version(match.group(2))):
            return False
    return True
