"""The embedding guide cannot rot: its example runs, and `import oska` stays light."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import oska

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "embedding.md"


def _first_python_block() -> str:
    match = re.search(r"```python\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    assert match, "docs/embedding.md has no python block"
    return match.group(1)


def test_the_example_in_the_guide_runs(tmp_path: Path) -> None:
    script = tmp_path / "example.py"
    script.write_text(_first_python_block(), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "OSKA_PROVIDER"}
    env["OSKA_STATE_DIR"] = str(tmp_path / "state")
    # The example reads the catalog by its path in the repository, so it runs from the root.
    done = subprocess.run(
        [sys.executable, str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "held for approval" in done.stdout
    assert "verified" in done.stdout


def test_oska_exposes_what_it_lists() -> None:
    assert set(oska.__all__) == {
        "__version__",
        "Settings",
        "load_settings",
        "build_services",
        "build_platform",
        "build_engine",
        "TurnResult",
        "Principal",
        "ScriptedModelProvider",
        "ReplayProvider",
    }
    for name in oska.__all__:
        assert getattr(oska, name) is not None


def test_import_oska_pulls_in_neither_the_api_nor_the_runtime() -> None:
    probe = (
        "import sys, oska\n"
        "assert 'fastapi' not in sys.modules\n"
        "assert 'oska.services' not in sys.modules\n"
        "for name in oska.__all__:\n"
        "    getattr(oska, name)\n"
        "assert 'fastapi' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-2000:]
