"""oska: governed execution of declared operational procedures.

The names below are the surface for embedding the runtime in a program. They are
resolved on first use, so `import oska` stays cheap and pulls in nothing of the
runtime, and nothing optional such as the HTTP application.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any

try:
    __version__ = version("oska")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"

if TYPE_CHECKING:
    from oska.config import Settings, load_settings
    from oska.contracts.common import TurnResult
    from oska.engine.factory import build_engine
    from oska.governance.identity import Principal
    from oska.planner.recording import ReplayProvider
    from oska.planner.scripted import ScriptedModelProvider
    from oska.services import build_platform, build_services

_EXPORTS: dict[str, str] = {
    "Settings": "oska.config",
    "load_settings": "oska.config",
    "build_services": "oska.services",
    "build_platform": "oska.services",
    "build_engine": "oska.engine.factory",
    "TurnResult": "oska.contracts.common",
    "Principal": "oska.governance.identity",
    "ScriptedModelProvider": "oska.planner.scripted",
    "ReplayProvider": "oska.planner.recording",
}

__all__ = [
    "Principal",
    "ReplayProvider",
    "ScriptedModelProvider",
    "Settings",
    "TurnResult",
    "__version__",
    "build_engine",
    "build_platform",
    "build_services",
    "load_settings",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *_EXPORTS])
