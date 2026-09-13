"""The example scenarios, run end to end through the conformance runner.

Each file is one session. The expectations are checked by `opsdeck.conformance`,
so the suite a caller runs and the suite the tests run are the same code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opsdeck.config import Settings
from opsdeck.conformance import ScenarioFile, load_scenarios, run_scenario

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "examples" / "ops" / "scenarios"
SCENARIOS = load_scenarios(SCENARIO_DIR)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario.name for scenario in SCENARIOS])
def test_scenario(scenario: ScenarioFile, tmp_path: Path, example_settings: Settings) -> None:
    result = run_scenario(scenario, example_settings, state_dir=tmp_path / "state")
    assert result.passed, "\n".join(result.failures)
