"""Build agent orchestrator. Top-level loop that:

1. Validates the lead is buildable (has GBP or existing site)
2. Researches → gathers assets → picks inspiration → builds HTML
3. Runs critics + technical gates in a loop until score thresholds met OR budget/time exhausted
4. Ships best-so-far + flags issues to the rep dialer for approval

Budget caps + daily fleet cap + per-tool timeout/retry/fallback per SPEC.md §8.

Step 1 STATUS: scaffolded only — full loop logic implemented in Step 6.
"""
from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path
from typing import Any


# ─── budgets ──────────────────────────────────────────────────────────────────
PER_BUILD_BUDGET_USD = float(os.environ.get("BUILD_AGENT_PER_BUILD_BUDGET", "7.00"))
PER_BUILD_DEADLINE_SEC = int(os.environ.get("BUILD_AGENT_DEADLINE_SEC", "720"))  # 12 min
DAILY_FLEET_CAP_USD = float(os.environ.get("BUILD_AGENT_DAILY_FLEET_CAP", "100.00"))
MAX_CRITIC_ITERATIONS = int(os.environ.get("BUILD_AGENT_MAX_ITERATIONS", "6"))


# ─── gate thresholds (SPEC §5) ────────────────────────────────────────────────
GATE_CODE_CRITIC_MIN = 90
GATE_VISION_CRITIC_MIN = 7.5
GATE_LIGHTHOUSE_PERF_MIN = 85
GATE_LIGHTHOUSE_A11Y_MIN = 90
GATE_REAL_ASSET_RATIO_MIN = 0.60


@dataclasses.dataclass
class BuildState:
    """Per-build mutable state owned by the orchestrator."""
    lead_id: str
    business_name: str
    started_at: float
    budget_remaining: float = PER_BUILD_BUDGET_USD
    iterations: int = 0
    research_brief: dict[str, Any] | None = None
    assets_manifest: dict[str, Any] | None = None
    inspiration_refs: list[str] = dataclasses.field(default_factory=list)
    current_html: str = ""
    best_html: str = ""
    best_score: float = 0.0
    last_verdict: dict[str, Any] | None = None
    fallbacks_used: list[str] = dataclasses.field(default_factory=list)

    def time_remaining(self) -> float:
        return max(0.0, PER_BUILD_DEADLINE_SEC - (time.time() - self.started_at))

    def budget_low(self) -> bool:
        return self.budget_remaining < 0.50

    def time_low(self) -> bool:
        return self.time_remaining() < 30.0


def build(lead: dict[str, Any]) -> dict[str, Any]:
    """Top-level entry. Returns {url, final_score, budget_used, ship_reason}."""
    raise NotImplementedError("Step 6 deliverable — orchestrator loop")


def precheck_buildable(lead: dict[str, Any]) -> dict[str, Any]:
    """Pre-filter: reject leads with no online presence so we don't waste budget."""
    raise NotImplementedError("Step 6 deliverable")


def check_daily_cap(db_path: Path) -> dict[str, Any]:
    """Read today's total spend across all builds; return {allowed, spent, cap}."""
    raise NotImplementedError("Step 6 deliverable")
