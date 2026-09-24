"""The example scenarios of every domain, run end to end through the conformance runner.

Each file is one session. The expectations are checked by `oska.conformance`,
so the suite a caller runs and the suite the tests run are the same code. The
same test runs over every domain under `examples/`, each with the catalog, the
connector bindings and the scenario directory of that domain and nothing else:
a scenario that passed only because another domain was loaded would say
something about the runtime's memory rather than about the domain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oska.config import Settings
from oska.conformance import ScenarioFile, load_scenarios, run_scenario

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
DOMAINS = sorted(path.name for path in EXAMPLES.iterdir() if (path / "scenarios").is_dir())

CASES = [
    (domain, scenario)
    for domain in DOMAINS
    for scenario in load_scenarios(EXAMPLES / domain / "scenarios")
]


def domain_settings(domain: str, state_dir: Path) -> Settings:
    """Settings over one example domain, with all state under a temporary directory."""
    root = EXAMPLES / domain
    return Settings(
        skills_roots=[root / "skills"],
        runtime_config=REPO_ROOT / "configs" / "runtime.json",
        model_config_path=REPO_ROOT / "configs" / "model.json",
        connectors_config=root / "configs" / "connectors.json",
        schemas_dir=REPO_ROOT / "schemas",
        scenarios_dir=root / "scenarios",
        state_dir=state_dir,
        provider="scripted",
    )


@pytest.mark.parametrize(
    ("domain", "scenario"),
    CASES,
    ids=[f"{domain}/{scenario.name}" for domain, scenario in CASES],
)
def test_scenario(domain: str, scenario: ScenarioFile, tmp_path: Path) -> None:
    settings = domain_settings(domain, tmp_path / "state")
    result = run_scenario(scenario, settings, state_dir=tmp_path / "state")
    assert result.passed, "\n".join(result.failures)
