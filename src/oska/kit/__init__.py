"""The kit: generating a pack or a platform, and checking or building one."""

from oska.kit.build import PackArtifact, build_pack
from oska.kit.check import PackReport, check_pack
from oska.kit.scaffold import DEFAULT_ENTITY, DEFAULT_HOST, DEFAULT_SYSTEM, new_pack, new_platform

__all__ = [
    "DEFAULT_ENTITY",
    "DEFAULT_HOST",
    "DEFAULT_SYSTEM",
    "PackArtifact",
    "PackReport",
    "build_pack",
    "check_pack",
    "new_pack",
    "new_platform",
]
