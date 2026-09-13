"""The layering, read from the source: who may import whom, and who owns a type.

The rule is checked statically, over the module tree as it is written. A module
belongs to the layer of its top-level package; `contracts` and `config` belong
to no layer and are used by all of them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "opsdeck"

LAYERS: dict[str, int] = {
    "connectors": 1,
    "ontology": 2,
    "catalog": 3,
    "governance": 4,
    "audit": 4,
    "index": 5,
    "router": 5,
    "planner": 5,
    "engine": 6,
    "api": 6,
    "cli": 6,
    "services": 6,
    "conformance": 6,
    "learning": 7,
}
SHARED = frozenset({"contracts", "config"})
COGNITION = frozenset({"index", "router", "planner"})
BELOW_COGNITION = frozenset({"connectors", "governance", "engine"})

# The channels and the assembly that builds them carry the proposal commands the
# interaction layer declares, so they reach the evolution layer where the engine
# and everything under it must not.
CHANNELS = frozenset({"api", "cli", "services", "conformance"})

# What the evolution layer may reach: the catalog it publishes into, and the
# provider interface it drafts wording with.
LEARNING_MAY_IMPORT = frozenset({"catalog", "learning"}) | SHARED
LEARNING_MAY_IMPORT_MODULES = frozenset({"opsdeck.planner.provider"})

# What cognition may reach: the catalog it ranks and plans over, the ontology it
# describes to a model, and the shared contracts and configuration.
COGNITION_MAY_IMPORT = frozenset({"catalog", "ontology"}) | COGNITION | SHARED


def _modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def _package_of(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def _imports(path: Path) -> list[str]:
    """Every `opsdeck` module this file imports, by dotted name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.append(node.module)
    return [name for name in imported if name.split(".")[0] == "opsdeck"]


def _target_package(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else None


def _crossings() -> list[tuple[str, str, str]]:
    """Every import one module makes of another, as (file, package, module)."""
    found: list[tuple[str, str, str]] = []
    for path in _modules():
        package = _package_of(path)
        if package not in LAYERS:
            continue
        for module in _imports(path):
            target = _target_package(module)
            if target is None or target == package:
                continue
            found.append((str(path.relative_to(PACKAGE_ROOT)), package, module))
    return found


def test_no_module_imports_from_a_layer_above_it() -> None:
    violations = [
        f"{where}: {package} (L{LAYERS[package]}) imports {module} "
        f"(L{LAYERS[_target_package(module) or '']})"
        for where, package, module in _crossings()
        if (target := _target_package(module)) in LAYERS
        and LAYERS[target] > LAYERS[package]
        and not (package in CHANNELS and target == "learning")
    ]
    assert violations == []


def test_the_evolution_layer_reaches_only_the_catalog_and_the_provider() -> None:
    violations = [
        f"{where}: {module}"
        for where, package, module in _crossings()
        if package == "learning"
        and _target_package(module) not in LEARNING_MAY_IMPORT
        and module not in LEARNING_MAY_IMPORT_MODULES
    ]
    assert violations == []


@pytest.mark.parametrize("package", sorted(COGNITION))
def test_cognition_reaches_no_connector_no_governance_and_no_engine(package: str) -> None:
    reached = {
        _target_package(module)
        for where, owner, module in _crossings()
        if owner == package
    }
    assert not reached & BELOW_COGNITION
    assert reached <= COGNITION_MAY_IMPORT


def _model_names(paths: list[Path]) -> dict[str, list[str]]:
    """Class name to the files defining it, for the pydantic models of these files."""
    defined: dict[str, list[str]] = {}
    bases: dict[str, set[str]] = {}
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef):
                continue
            defined.setdefault(node.name, []).append(str(path))
            bases[node.name] = {
                base.id for base in node.bases if isinstance(base, ast.Name)
            }
    models: set[str] = set()
    growing = True
    while growing:
        growing = False
        for name, inherited in bases.items():
            if name in models:
                continue
            if "BaseModel" in inherited or inherited & models:
                models.add(name)
                growing = True
    return {name: paths for name, paths in defined.items() if name in models}


def _class_names(paths: list[Path]) -> dict[str, list[str]]:
    """Every class defined in these files, by name."""
    defined: dict[str, list[str]] = {}
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                defined.setdefault(node.name, []).append(str(path))
    return defined


def test_a_shared_type_is_declared_in_the_contracts_and_nowhere_else() -> None:
    contracts = sorted((PACKAGE_ROOT / "contracts").rglob("*.py"))
    elsewhere = [path for path in _modules() if path not in contracts]
    declared = _model_names(contracts)
    assert "Skill" in declared and "Plan" in declared
    redeclared = {
        name: paths for name, paths in _class_names(elsewhere).items() if name in declared
    }
    assert redeclared == {}
