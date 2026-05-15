"""Smoke test — ensures the package scaffolding imports cleanly.

Step 1 acceptance criterion. Run with: python -m pytest build_agent/tests/test_imports.py -v
Or directly: python -m build_agent.tests.test_imports
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


MODULES = [
    "build_agent",
    "build_agent.orchestrator",
    "build_agent.agents",
    "build_agent.agents.researcher",
    "build_agent.agents.asset_gatherer",
    "build_agent.agents.inspiration_picker",
    "build_agent.agents.builder",
    "build_agent.agents.critic_code",
    "build_agent.agents.critic_vision",
    "build_agent.agents.deployer",
    "build_agent.agents.notifier",
    "build_agent.tools",
    "build_agent.tools.outscraper",
    "build_agent.tools.brave",
    "build_agent.tools.existing_site_scraper",
    "build_agent.tools.palette",
    "build_agent.tools.flux",
    "build_agent.tools.image_compress",
    "build_agent.tools.technical_gates",
]


def test_all_modules_import():
    failures = []
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            failures.append((name, repr(e)))
    if failures:
        raise AssertionError(f"{len(failures)} module(s) failed to import: {failures}")


def test_orchestrator_constants_present():
    from build_agent import orchestrator as o
    assert o.PER_BUILD_BUDGET_USD > 0
    assert o.PER_BUILD_DEADLINE_SEC > 0
    assert o.DAILY_FLEET_CAP_USD > 0
    assert o.GATE_VISION_CRITIC_MIN == 7.5
    assert o.GATE_CODE_CRITIC_MIN == 90


def test_vision_critic_rubric_weights_sum_to_one():
    from build_agent.agents import critic_vision as cv
    total = sum(cv.RUBRIC_WEIGHTS.values())
    assert abs(total - 1.0) < 1e-6, f"Vision critic rubric weights sum to {total}, not 1.0"


if __name__ == "__main__":
    test_all_modules_import()
    test_orchestrator_constants_present()
    test_vision_critic_rubric_weights_sum_to_one()
    print("OK")
